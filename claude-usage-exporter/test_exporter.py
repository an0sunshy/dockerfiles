#!/usr/bin/env python3
"""Tests for the Claude usage exporter's durable, monotonic token counter.

Run: python3 -m unittest -v   (from this directory)

The exporter is a `counter`, so its emitted values must never decrease. The
transcript store on disk shrinks (sessions cleared/pruned, `/clear`), so a naive
recompute-from-disk drops -> Prometheus reads a reset -> increase()/rate() inflate.
These tests pin the two guarantees that fix it: deletions never subtract, and new
activity after a prune is still counted (i.e. not a value that merely ratchets).

They also pin the property the pricing split depends on: EVERY model is banked,
including ids this build has never heard of. Prices live server-side, so an
unknown id is the normal case; anything dropped here can never be priced later.
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

# Token counts written by write_msg below, summed across all five buckets.
ONE_MSG_TOKENS = 1000 + 500 + 200000 + 100


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


def total_tokens(state):
    return sum(sum(v.values()) for v in state["tokens"].values())


def total_messages(state):
    return sum(state["messages"].values())


def models_in(state):
    return {k.split("\x1f")[0] for k in state["tokens"]}


class MonotonicCounterTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_deletion_does_not_decrease_counter(self):
        """The exact bug: deleting a transcript must NOT lower the counter."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        t1 = total_tokens(state)
        self.assertGreater(t1, 0)

        os.remove(os.path.join(self.d, "a.jsonl"))  # session cleared / pruned
        cue.update_state(state, self.d)
        self.assertEqual(total_tokens(state), t1)   # not dropped

    def test_reparse_same_disk_is_idempotent(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        t1, m1 = total_tokens(state), total_messages(state)
        cue.update_state(state, self.d)          # nothing new on disk
        self.assertEqual(total_tokens(state), t1)
        self.assertEqual(total_messages(state), m1)

    def test_new_activity_after_prune_is_counted(self):
        """Distinguishes a real accumulator from a naive high-water ratchet:
        after a prune, brand-new usage must still add on top."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        t1 = total_tokens(state)

        os.remove(os.path.join(self.d, "a.jsonl"))
        write_msg(self.d, "c.jsonl", "msg-c")    # new session after the prune
        cue.update_state(state, self.d)
        self.assertEqual(total_tokens(state), t1 + ONE_MSG_TOKENS)

    def test_persist_and_reload_roundtrip(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        reloaded = cue.load_state(path)
        self.assertEqual(total_tokens(reloaded), total_tokens(state))
        self.assertEqual(total_messages(reloaded), total_messages(state))
        # dedup memory survived: re-parsing the same disk adds nothing
        cue.update_state(reloaded, self.d)
        self.assertEqual(total_tokens(reloaded), total_tokens(state))

    def test_durable_across_restart_and_deletion(self):
        """Simulate a container restart (fresh process loads state file) that
        coincides with a pruned transcript: the banked value must persist."""
        write_msg(self.d, "a.jsonl", "msg-a")
        write_msg(self.d, "b.jsonl", "msg-b")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        t1 = total_tokens(state)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        os.remove(os.path.join(self.d, "a.jsonl"))   # pruned while "down"
        restarted = cue.load_state(path)             # fresh process
        cue.update_state(restarted, self.d)
        self.assertEqual(total_tokens(restarted), t1)  # still reflects both msgs

    def test_load_missing_state_is_empty(self):
        state = cue.load_state(os.path.join(self.d, "does-not-exist.json"))
        self.assertEqual(total_tokens(state), 0)
        self.assertEqual(total_messages(state), 0)

    def test_load_corrupt_state_is_empty(self):
        path = os.path.join(self.d, "state.json")
        with open(path, "w") as f:
            f.write("{not valid json")
        state = cue.load_state(path)   # must not raise
        self.assertEqual(total_tokens(state), 0)

    def test_load_wrong_shape_state_is_empty(self):
        """Valid JSON, wrong schema (tokens value isn't a dict) must degrade to
        empty via the load guard, not crash the process."""
        path = os.path.join(self.d, "state.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "tokens": {"m\x1fe": 5}, "messages": {}}, f)
        state = cue.load_state(path)   # must not raise
        self.assertEqual(total_tokens(state), 0)
        self.assertEqual(total_messages(state), 0)

    def test_empty_projects_dir_does_not_zero_counter(self):
        """All transcripts vanishing (empty dir) must not zero a loaded counter:
        nothing new -> dirty False -> no save -> in-memory total preserved."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        t1 = total_tokens(state)
        self.assertGreater(t1, 0)
        empty = tempfile.mkdtemp()
        _, dirty = cue.update_state(state, empty)
        self.assertFalse(dirty)
        self.assertEqual(total_tokens(state), t1)   # not zeroed

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

    def test_dirty_flag_tracks_new_activity(self):
        """update_state reports dirty=True only when it folds in new usage, so
        --serve can skip rewriting a growing state file on idle scrapes."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        _, dirty_first = cue.update_state(state, self.d)
        self.assertTrue(dirty_first)
        _, dirty_noop = cue.update_state(state, self.d)   # same disk
        self.assertFalse(dirty_noop)
        write_msg(self.d, "b.jsonl", "msg-b")
        _, dirty_after = cue.update_state(state, self.d)
        self.assertTrue(dirty_after)

    def test_render_emits_counter_with_accumulated_value(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.render(state, 1, 0.01, "testhost")
        self.assertIn("# TYPE claude_code_tokens_total counter", text)
        self.assertIn('host="testhost"', text)
        self.assertIn("claude-opus-4-8", text)

    def test_synthetic_model_is_skipped(self):
        """"<synthetic>" records carry no real usage and must not create a series
        (they would otherwise show up server-side as an unpriced model)."""
        write_msg(self.d, "a.jsonl", "msg-a", model="<synthetic>")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertEqual(total_messages(state), 0)
        self.assertEqual(models_in(state), set())


class ModelNamingTest(unittest.TestCase):
    """normalize_model decides the `model` label, which is the join key the
    server's price rules match on. A name that normalizes wrong is unpriced."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_dated_model_snapshot_normalizes(self):
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-haiku-4-5-20251001")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertEqual(models_in(state), {"claude-haiku-4-5"})

    def test_normalize_strips_nothing_but_a_dated_snapshot(self):
        """The whole normalization rule, pinned directly.

        A dot-release must stay distinct from its parent: they need not share a
        price, and the server prices per id.
        """
        self.assertEqual(cue.normalize_model("claude-opus-4-5-20260301"),
                         "claude-opus-4-5")          # dated snapshot: stripped
        for unchanged in ("claude-opus-4-5", "claude-opus-5-1",
                          "claude-opus-5-fast", "claude-opus-5[1m]",
                          "claude-opus-5-2026030"):  # 7 digits, not a snapshot
            self.assertEqual(cue.normalize_model(unchanged), unchanged)

    def test_dated_snapshot_normalizes_back_onto_its_base_id(self):
        """Anthropic ships ids both bare and dated (claude-haiku-4-5 /
        claude-haiku-4-5-20251001). If a base id stopped absorbing its own dated
        form the usage would split across two model labels, and the server would
        price only one of them."""
        for base in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                     "claude-fable-5", "claude-opus-5-1"):
            self.assertEqual(cue.normalize_model(base), base)
            self.assertEqual(cue.normalize_model(base + "-20260301"), base)

    def test_local_lane_alias_lands_on_the_official_id(self):
        """A legacy local lane name ("smart") must be remapped to the official
        OpenRouter id, which is what the server's price rules key on."""
        write_msg(self.d, "a.jsonl", "msg-a", model="smart")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertEqual(models_in(state), {"qwen/qwen3.6-35b-a3b"})

    def test_empty_model_becomes_unknown(self):
        self.assertEqual(cue.normalize_model(None), "unknown")
        self.assertEqual(cue.normalize_model(""), "unknown")


class ServerSidePricingContractTest(unittest.TestCase):
    """The exporter must ship NO prices and drop NO tokens.

    Both halves matter. Dropping a token loses usage the server can never price;
    shipping a price recreates the second registry that made every model release
    a fleet-wide image rollout.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_unknown_model_is_banked_not_deferred(self):
        """The inversion of the old behaviour, and the reason for the split.

        A model this build has never heard of must be counted immediately. It
        used to be withheld pending a PRICING entry, which meant a fleet-wide
        image rollout raced Claude Code's ~30d transcript retention.
        """
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-nextgen-9")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertEqual(models_in(state), {"claude-nextgen-9"})
        self.assertEqual(total_messages(state), 1)
        self.assertEqual(total_tokens(state), ONE_MSG_TOKENS)

    def test_unknown_vendor_model_is_banked(self):
        """Not just unseen Anthropic ids — any vendor. The server decides what
        it can price; the client does not get a veto."""
        write_msg(self.d, "a.jsonl", "msg-a", model="somelab/some-model-v3")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertEqual(models_in(state), {"somelab/some-model-v3"})
        self.assertEqual(total_tokens(state), ONE_MSG_TOKENS)

    def test_unknown_model_is_counted_exactly_once(self):
        """Banking an unknown model must still mark it seen, or every scrape
        re-adds it and the counter runs away."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-nextgen-9")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        cue.update_state(state, self.d)
        cue.update_state(state, self.d)
        self.assertEqual(total_messages(state), 1)
        self.assertEqual(total_tokens(state), ONE_MSG_TOKENS)

    def test_no_price_table_is_exposed(self):
        """A price table anywhere in this module is the regression: it would put
        prices back in the image and re-create the two-registry problem."""
        for gone in ("PRICING", "TIER_PRICING", "resolve_price",
                     "imputed_models", "CACHE_READ_MULT",
                     "CACHE_WRITE_5M_MULT", "CACHE_WRITE_1H_MULT"):
            self.assertFalse(hasattr(cue, gone),
                             f"{gone} must not exist: prices live server-side")

    def test_render_emits_no_cost_or_pricing_metrics(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.render(state, 1, 0.01, "")
        for gone in ("claude_code_cost_usd_total",
                     "claude_code_unpriced_model_info",
                     "claude_code_imputed_price_model_info",
                     "claude_code_usage_exporter_unpriced_models",
                     "claude_code_usage_exporter_imputed_price_models"):
            self.assertNotIn(gone, text)

    def test_render_still_emits_the_series_the_server_prices(self):
        """The server joins on (model, type), so every bucket must be present
        even when zero — a missing leg silently drops that leg's cost."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.render(state, 1, 0.01, "")
        for t in ("input", "output", "cache_read",
                  "cache_write_5m", "cache_write_1h"):
            self.assertIn(f'type="{t}"', text)

    def test_state_from_a_pricing_era_build_upgrades_without_resetting(self):
        """The rollout path. An old state file carries a "cost" key this build
        does not know. It must be ignored, NOT rejected — rejecting it would
        drop the seen-set and re-bank every message still on disk, double
        counting against the counter the server has already scraped."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        with open(path) as f:
            data = json.load(f)
        data["cost"] = {"claude-opus-4-8\x1fcli": 1.2345}   # pricing-era key
        with open(path, "w") as f:
            json.dump(data, f)

        reloaded = cue.load_state(path)
        self.assertEqual(total_tokens(reloaded), ONE_MSG_TOKENS)
        self.assertEqual(total_messages(reloaded), 1)
        cue.update_state(reloaded, self.d)      # same disk, already seen
        self.assertEqual(total_messages(reloaded), 1)   # no double count

    def test_previously_deferred_usage_is_recovered_on_upgrade(self):
        """Usage an old build deferred was never marked seen, so this build
        picks it up on first parse — as long as the transcript is still there."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-nextgen-9")
        # A pricing-era state file: the message was seen on disk but withheld,
        # so it appears in neither `seen` nor the token totals.
        state = cue.empty_state()
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        upgraded = cue.load_state(path)
        cue.update_state(upgraded, self.d)
        self.assertEqual(total_tokens(upgraded), ONE_MSG_TOKENS)

    def test_human_summary_reports_tokens_without_prices(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.human_summary(state)
        self.assertIn("TOTAL tokens", text)
        self.assertIn("claude-opus-4-8", text)
        self.assertNotIn("$0", text)          # no fabricated dollar figures


if __name__ == "__main__":
    unittest.main()
