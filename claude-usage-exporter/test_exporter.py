#!/usr/bin/env python3
"""Tests for the Claude usage exporter's durable, monotonic counter.

Run: python3 -m unittest -v   (from this directory)

The exporter is a `counter`, so its emitted values must never decrease. The
transcript store on disk shrinks (sessions cleared/pruned, `/clear`), so a naive
recompute-from-disk drops -> Prometheus reads a reset -> increase()/rate() inflate.
These tests pin the two guarantees that fix it: deletions never subtract, and new
activity after a prune is still counted (i.e. not a value that merely ratchets).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    spec = importlib.util.spec_from_file_location(
        "cue", os.path.join(HERE, "claude-usage-exporter.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cue = _load()

# Cost of one canonical opus-4-8 message, computed from PRICING the same way the
# exporter does, so tests assert exact deltas rather than magic numbers.
#   input 1000*$5 + output 500*$25 + cache_read 200000*$5*0.1
#   + cache_write_5m 100*$5*1.25  (all /1e6)
ONE_MSG_USD = (1000 * 5 + 500 * 25 + 200000 * 5 * 0.1 + 100 * 5 * 1.25) / 1_000_000.0


def write_msg(d, name, msg_id, model="claude-opus-4-8", req=None, session=None,
              entry="cli"):
    """Append one assistant message with usage to transcript `name` in dir `d`."""
    rec = {
        "type": "assistant",
        "requestId": req if req is not None else f"req-{name}-{msg_id}",
        "sessionId": session or f"sess-{name}",
        "entrypoint": entry,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 200000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
        },
    }
    with open(os.path.join(d, name), "a") as f:
        f.write(json.dumps(rec) + "\n")


def total_cost(state):
    return sum(state["cost"].values())


def total_tokens(state):
    return sum(sum(v.values()) for v in state["tokens"].values())


def total_messages(state):
    return sum(state["messages"].values())


class MonotonicCounterTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_deletion_does_not_decrease_counter(self):
        """The exact bug: deleting a transcript must NOT lower the counter."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        c1, t1 = total_cost(state), total_tokens(state)
        self.assertGreater(c1, 0)

        os.remove(os.path.join(self.d, "a.jsonl"))  # session cleared / pruned
        cue.update_state(state, self.d)
        self.assertEqual(total_cost(state), c1)   # not dropped
        self.assertEqual(total_tokens(state), t1)

    def test_reparse_same_disk_is_idempotent(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        c1, m1 = total_cost(state), total_messages(state)
        cue.update_state(state, self.d)          # nothing new on disk
        self.assertAlmostEqual(total_cost(state), c1)
        self.assertEqual(total_messages(state), m1)

    def test_new_activity_after_prune_is_counted(self):
        """Distinguishes a real accumulator from a naive high-water ratchet:
        after a prune, brand-new usage must still add on top."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        c1 = total_cost(state)

        os.remove(os.path.join(self.d, "a.jsonl"))
        write_msg(self.d, "c.jsonl", "msg-c")    # new session after the prune
        cue.update_state(state, self.d)
        self.assertAlmostEqual(total_cost(state), c1 + ONE_MSG_USD, places=6)

    def test_persist_and_reload_roundtrip(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        reloaded = cue.load_state(path)
        self.assertAlmostEqual(total_cost(reloaded), total_cost(state))
        self.assertEqual(total_tokens(reloaded), total_tokens(state))
        self.assertEqual(total_messages(reloaded), total_messages(state))
        # dedup memory survived: re-parsing the same disk adds nothing
        cue.update_state(reloaded, self.d)
        self.assertAlmostEqual(total_cost(reloaded), total_cost(state))

    def test_durable_across_restart_and_deletion(self):
        """Simulate a container restart (fresh process loads state file) that
        coincides with a pruned transcript: the banked value must persist."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        c1 = total_cost(state)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        os.remove(os.path.join(self.d, "a.jsonl"))   # pruned while "down"
        restarted = cue.load_state(path)             # fresh process
        cue.update_state(restarted, self.d)
        self.assertEqual(total_cost(restarted), c1)  # still reflects both msgs

    def test_load_missing_state_is_empty(self):
        state = cue.load_state(os.path.join(self.d, "does-not-exist.json"))
        self.assertEqual(total_cost(state), 0)
        self.assertEqual(total_messages(state), 0)

    def test_load_corrupt_state_is_empty(self):
        path = os.path.join(self.d, "state.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        state = cue.load_state(path)   # must not raise
        self.assertEqual(total_cost(state), 0)

    def test_load_wrong_shape_state_is_empty(self):
        """Valid JSON, wrong schema (tokens value isn't a dict) must degrade to
        empty via the load guard, not crash the process."""
        path = os.path.join(self.d, "state.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "tokens": {"m\x1fe": 5},
                       "cost": {}, "messages": {}}, f)
        state = cue.load_state(path)   # must not raise
        self.assertEqual(total_cost(state), 0)
        self.assertEqual(total_messages(state), 0)

    def test_empty_projects_dir_does_not_zero_counter(self):
        """All transcripts vanishing (empty dir) must not zero a loaded counter:
        nothing new -> dirty False -> no save -> in-memory total preserved."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        c1 = total_cost(state)
        self.assertGreater(c1, 0)
        empty = tempfile.mkdtemp()
        _, _, dirty = cue.update_state(state, empty)
        self.assertFalse(dirty)
        self.assertEqual(total_cost(state), c1)   # not zeroed

    def test_print_mode_never_persists_state(self):
        """--print is a read-only debug view (may run via `docker exec` beside a
        serving process); it must never write the state file."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state_path = os.path.join(self.d, "state.json")
        script = os.path.join(HERE, "claude-usage-exporter.py")
        r = subprocess.run(
            [sys.executable, script, "--print",
             "--projects-dir", self.d, "--state-file", state_path],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(state_path))

    def test_unpriced_model_is_deferred_not_banked(self):
        """An unknown model must not be banked at $0 (which would freeze its cost
        forever). It stays uncounted until PRICING gains it, then is picked up."""
        write_msg(self.d, "a.jsonl", "msg-a", model="future-model-9")
        state = cue.empty_state()
        unpriced, _, _ = cue.update_state(state, self.d)
        self.assertIn("future-model-9", unpriced)
        self.assertEqual(total_cost(state), 0)
        self.assertEqual(total_messages(state), 0)   # not banked

        # Operator adds pricing and redeploys the image; message is still on disk.
        cue.PRICING["future-model-9"] = (3.0, 15.0)
        self.addCleanup(lambda: cue.PRICING.pop("future-model-9", None))
        unpriced2, _, _ = cue.update_state(state, self.d)
        self.assertNotIn("future-model-9", unpriced2)
        self.assertGreater(total_cost(state), 0)
        self.assertEqual(total_messages(state), 1)   # counted exactly once

    def test_keyless_message_not_double_counted(self):
        """A usage record with no id/requestId still must dedup across runs
        (identity falls back to path#line), or every scrape re-adds it."""
        rec = {
            "type": "assistant", "requestId": None, "sessionId": "s1",
            "entrypoint": "cli",
            "message": {"id": None, "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 1000, "output_tokens": 500,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation": {}}},
        }
        with open(os.path.join(self.d, "a.jsonl"), "w") as f:
            f.write(json.dumps(rec) + "\n")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        cue.update_state(state, self.d)   # second scrape, same line
        self.assertEqual(total_messages(state), 1)

    def test_dated_model_snapshot_normalizes(self):
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-haiku-4-5-20251001")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        models = {k.split("\x1f")[0] for k in state["cost"]}
        self.assertEqual(models, {"claude-haiku-4-5"})

    def test_local_lane_alias_priced_under_official_name(self):
        """A legacy local lane name ("smart") must be remapped to the official
        OpenRouter id and priced at its imputed rates, not deferred as unpriced."""
        write_msg(self.d, "a.jsonl", "msg-a", model="smart")
        state = cue.empty_state()
        unpriced, _, _ = cue.update_state(state, self.d)
        self.assertEqual(unpriced, set())
        models = {k.split("\x1f")[0] for k in state["cost"]}
        self.assertEqual(models, {"qwen/qwen3.6-35b-a3b"})
        p_in, p_out = cue.PRICING["qwen/qwen3.6-35b-a3b"]
        expected = (1000 * p_in + 500 * p_out + 200000 * p_in * 0.1
                    + 100 * p_in * 1.25) / 1_000_000.0
        self.assertAlmostEqual(total_cost(state), expected, places=9)

    def test_dirty_flag_tracks_new_activity(self):
        """update_state reports dirty=True only when it folds in new usage, so
        --serve can skip rewriting a growing state file on idle scrapes."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        _, _, dirty_first = cue.update_state(state, self.d)
        self.assertTrue(dirty_first)
        _, _, dirty_noop = cue.update_state(state, self.d)   # same disk
        self.assertFalse(dirty_noop)
        write_msg(self.d, "b.jsonl", "msg-b")
        _, _, dirty_after = cue.update_state(state, self.d)
        self.assertTrue(dirty_after)

    def test_render_emits_counter_with_accumulated_value(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.render(state, set(), 1, 0.01, "testhost")
        self.assertIn("# TYPE claude_code_cost_usd_total counter", text)
        self.assertIn('host="testhost"', text)
        self.assertIn('claude-opus-4-8', text)


if __name__ == "__main__":
    unittest.main()
