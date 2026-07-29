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

# Cost of one canonical message (the token counts write_msg emits) at the given
# $/1M rates, computed the same way the exporter does, so tests assert exact
# deltas rather than magic numbers: input 1000 + output 500 +
# cache_read 200000 (0.1x) + cache_write_5m 100 (1.25x).
def one_msg_cost(p_in, p_out):
    return (1000 * p_in + 500 * p_out + 200000 * p_in * 0.1
            + 100 * p_in * 1.25) / 1_000_000.0


ONE_MSG_USD = one_msg_cost(5.0, 25.0)  # canonical opus-4-8 message


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
        _, _, _, dirty = cue.update_state(state, empty)
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
        unpriced, _, _, _ = cue.update_state(state, self.d)
        self.assertIn("future-model-9", unpriced)
        self.assertEqual(total_cost(state), 0)
        self.assertEqual(total_messages(state), 0)   # not banked

        # Operator adds pricing and redeploys the image; message is still on disk.
        cue.PRICING["future-model-9"] = (3.0, 15.0)
        self.addCleanup(lambda: cue.PRICING.pop("future-model-9", None))
        unpriced2, _, _, _ = cue.update_state(state, self.d)
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

    def test_normalize_strips_nothing_but_a_dated_snapshot(self):
        """The whole normalization rule, pinned directly.

        This replaces an invariant that asserted no PRICING key was a prefix of
        another - a guard made necessary by the old `startswith(known + "-")`
        matcher, and one that by its own admission could not catch the hazard
        that actually mattered (a key absorbing a future id absent from the
        table). The matcher no longer does prefix matching, so the rule it
        guarded is gone; this pins the replacement instead.
        """
        self.assertEqual(cue.normalize_model("claude-opus-4-5-20260301"),
                         "claude-opus-4-5")          # dated snapshot: stripped
        for unchanged in ("claude-opus-4-5", "claude-opus-5-1",
                          "claude-opus-5-fast", "claude-opus-5[1m]",
                          "claude-opus-5-2026030"):  # 7 digits, not a snapshot
            self.assertEqual(cue.normalize_model(unchanged), unchanged)

    def test_dated_snapshot_of_every_pricing_key_resolves_to_itself(self):
        """A dated snapshot must normalize back onto its base key.

        Anthropic ships ids both bare and dated (claude-haiku-4-5 /
        claude-haiku-4-5-20251001). If a key stopped absorbing its own dated
        form the usage would split across two model labels, one of them
        unpriced. normalize_model had no direct test coverage before this.
        """
        for key in cue.PRICING:
            if "/" in key:
                continue  # local OpenRouter ids are not date-snapshotted
            self.assertEqual(cue.normalize_model(key), key)
            self.assertEqual(cue.normalize_model(key + "-20260301"), key)

    def test_local_lane_alias_priced_under_official_name(self):
        """A legacy local lane name ("smart") must be remapped to the official
        OpenRouter id and priced at its imputed rates, not deferred as unpriced."""
        write_msg(self.d, "a.jsonl", "msg-a", model="smart")
        state = cue.empty_state()
        unpriced, _, _, _ = cue.update_state(state, self.d)
        self.assertEqual(unpriced, set())
        models = {k.split("\x1f")[0] for k in state["cost"]}
        self.assertEqual(models, {"qwen/qwen3.6-35b-a3b"})
        p_in, p_out = cue.PRICING["qwen/qwen3.6-35b-a3b"]
        self.assertAlmostEqual(total_cost(state), one_msg_cost(p_in, p_out),
                               places=9)

    def test_official_id_local_model_priced_directly(self):
        """A local model recorded under its official OpenRouter id needs no
        alias and must price at its imputed rates, not defer as unpriced."""
        write_msg(self.d, "a.jsonl", "msg-a", model="deepseek/deepseek-v4-flash")
        state = cue.empty_state()
        unpriced, _, _, _ = cue.update_state(state, self.d)
        self.assertEqual(unpriced, set())
        models = {k.split("\x1f")[0] for k in state["cost"]}
        self.assertEqual(models, {"deepseek/deepseek-v4-flash"})
        p_in, p_out = cue.PRICING["deepseek/deepseek-v4-flash"]
        self.assertAlmostEqual(total_cost(state), one_msg_cost(p_in, p_out),
                               places=9)

    def test_dirty_flag_tracks_new_activity(self):
        """update_state reports dirty=True only when it folds in new usage, so
        --serve can skip rewriting a growing state file on idle scrapes."""
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        _, _, _, dirty_first = cue.update_state(state, self.d)
        self.assertTrue(dirty_first)
        _, _, _, dirty_noop = cue.update_state(state, self.d)   # same disk
        self.assertFalse(dirty_noop)
        write_msg(self.d, "b.jsonl", "msg-b")
        _, _, _, dirty_after = cue.update_state(state, self.d)
        self.assertTrue(dirty_after)

    def test_render_emits_counter_with_accumulated_value(self):
        write_msg(self.d, "a.jsonl", "msg-a")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        text = cue.render(state, set(), {}, 1, 0.01, "testhost")
        self.assertIn("# TYPE claude_code_cost_usd_total counter", text)
        self.assertIn('host="testhost"', text)
        self.assertIn('claude-opus-4-8', text)


class TierFallbackPricingTest(unittest.TestCase):
    """A model released today must be counted today.

    Before the fallback, an id absent from PRICING was deferred until someone
    edited the table, pushed, rebuilt the image, and redeployed every host -
    which left claude-opus-5 uncounted for four days. These pin that a
    Claude-shaped id prices from its tier immediately, that the estimate is
    reported as an estimate, and that the deferral still protects the cases
    where guessing would be wrong.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_unreleased_claude_model_is_priced_from_its_tier(self):
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-9")
        state = cue.empty_state()
        unpriced, imputed, _, _ = cue.update_state(state, self.d)
        self.assertEqual(unpriced, set())            # not deferred
        self.assertEqual(imputed.get("claude-opus-9"), "opus")     # but flagged as estimated
        self.assertEqual(total_messages(state), 1)
        self.assertAlmostEqual(total_cost(state),
                               one_msg_cost(*cue.TIER_PRICING["opus"]),
                               places=9)

    def test_each_tier_uses_its_own_rate(self):
        """A sonnet-shaped id must not be priced at the opus rate."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-sonnet-9")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        self.assertAlmostEqual(total_cost(state),
                               one_msg_cost(*cue.TIER_PRICING["sonnet"]),
                               places=9)

    def test_dated_snapshot_of_unknown_model_collapses_to_one_series(self):
        """Two snapshots of the same unreleased model are one series, not two."""
        write_msg(self.d, "a.jsonl", "m1", model="claude-haiku-9-20260901")
        write_msg(self.d, "a.jsonl", "m2", model="claude-haiku-9-20261101")
        state = cue.empty_state()
        _, imputed, _, _ = cue.update_state(state, self.d)
        models = {k.split("\x1f")[0] for k in state["cost"]}
        self.assertEqual(models, {"claude-haiku-9"})
        self.assertEqual(imputed, {"claude-haiku-9": "haiku"})

    def test_pinned_model_is_not_reported_as_imputed(self):
        """An exact PRICING hit must never be flagged as an estimate, or
        ClaudeUsageImputedPrice would fire forever on ordinary traffic."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-4-8")
        state = cue.empty_state()
        unpriced, imputed, _, _ = cue.update_state(state, self.d)
        self.assertEqual(unpriced, set())
        self.assertEqual(imputed, {})

    def test_context_variant_stays_unpriced(self):
        """A bracketed long-context id bills at a premium, so pricing it at the
        base rate would undercount. Deferring it is the deliberate choice - the
        usage is recoverable once a real rate is pinned."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-5[1m]")
        state = cue.empty_state()
        unpriced, imputed, _, _ = cue.update_state(state, self.d)
        self.assertIn("claude-opus-5[1m]", unpriced)
        self.assertEqual(imputed, {})
        self.assertEqual(total_cost(state), 0)

    def test_non_claude_model_is_still_deferred(self):
        """The family fallback must not swallow an unrelated vendor's id."""
        write_msg(self.d, "a.jsonl", "msg-a", model="mistral/large-3")
        state = cue.empty_state()
        unpriced, imputed, _, _ = cue.update_state(state, self.d)
        self.assertIn("mistral/large-3", unpriced)
        self.assertEqual(imputed, {})

    def test_every_tier_rate_is_grounded_in_a_pinned_price(self):
        """A tier rate is only defensible as the rate that tier's pinned models
        already share. An invented rate would silently misprice every future
        release in that line."""
        for tier, rate in cue.TIER_PRICING.items():
            matching = []
            for key, pinned in cue.PRICING.items():
                m = cue._MODEL_RE.match(key)
                if m and m.group(1) == tier:
                    matching.append(pinned)
            self.assertTrue(matching, f"tier {tier} has no pinned model backing it")
            self.assertIn(rate, matching,
                          f"tier {tier} rate {rate} matches no pinned {tier} price")

    def test_fable_is_deliberately_not_imputed(self):
        """Fable 5 is $10/$50 and breaks the tier pattern the other lines share,
        so an unrecognised fable release must defer rather than be guessed.
        See ansible-scripts docs/model-pricing-automation.md."""
        self.assertNotIn("fable", cue.TIER_PRICING)
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-fable-9")
        state = cue.empty_state()
        unpriced, imputed, _, _ = cue.update_state(state, self.d)
        self.assertIn("claude-fable-9", unpriced)
        self.assertEqual(imputed, {})
        self.assertEqual(total_cost(state), 0)

    def test_imputed_set_persists_across_runs(self):
        """The imputed set is a STOCK, not a flow: it must stay populated for as
        long as estimated dollars sit in the counter.

        An imputed message is banked and marked seen, so deriving this from what
        a run folded in reports the model once and zero forever after. The gauge
        would collapse one scrape after the model appeared and the alert's for-
        clock would reset constantly, making ClaudeUsageImputedPrice unfireable.
        Contrast unpriced, which is safe per-run only because deferred messages
        are deliberately never marked seen.
        """
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-9")
        state = cue.empty_state()
        for run in range(3):
            _, imputed, _, _ = cue.update_state(state, self.d)
            self.assertEqual(imputed, {"claude-opus-9": "opus"},
                             f"imputed set emptied on run {run + 1}")

    def test_imputed_set_survives_a_restart(self):
        """A fresh process loading the state file must still report the banked
        dollars as estimated - otherwise a restart erases the only record that
        any cost was imputed, and nothing can answer 'which totals are guesses'."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-9")
        state = cue.empty_state()
        cue.update_state(state, self.d)
        path = os.path.join(self.d, "state.json")
        cue.save_state(path, state)

        os.remove(os.path.join(self.d, "a.jsonl"))   # transcript pruned meanwhile
        restarted = cue.load_state(path)
        _, imputed, _, _ = cue.update_state(restarted, self.d)
        self.assertEqual(imputed, {"claude-opus-9": "opus"})

    def test_pinning_an_imputed_model_clears_it_without_double_counting(self):
        """The intended lifecycle: imputed on release day, exact once someone
        pins the published rate. Pinning - and ONLY pinning - clears the flag,
        and the already-banked messages must not be counted a second time."""
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-9")
        state = cue.empty_state()
        _, imputed, _, _ = cue.update_state(state, self.d)
        self.assertEqual(imputed, {"claude-opus-9": "opus"})
        banked = total_cost(state)
        self.assertAlmostEqual(banked, one_msg_cost(*cue.TIER_PRICING["opus"]),
                               places=9)

        cue.PRICING["claude-opus-9"] = (8.0, 40.0)   # operator pins the real rate
        self.addCleanup(lambda: cue.PRICING.pop("claude-opus-9", None))
        _, imputed2, _, _ = cue.update_state(state, self.d)
        self.assertEqual(imputed2, {})
        self.assertEqual(total_messages(state), 1)          # not re-counted
        self.assertAlmostEqual(total_cost(state), banked, places=9)
        # The banked dollars keep the tier rate: monotonic counters cannot be
        # restated, which is why the alert must be reachable in the first place.
        self.assertNotAlmostEqual(total_cost(state), one_msg_cost(8.0, 40.0),
                                  places=9)

    def test_dot_release_is_not_folded_into_its_parent(self):
        """claude-opus-5-1 must not collapse into claude-opus-5. It used to,
        landing on the wrong model label at the parent's rate and reaching
        neither alert. It should now stand as its own imputed series."""
        self.assertEqual(cue.normalize_model("claude-opus-5-1"), "claude-opus-5-1")
        write_msg(self.d, "a.jsonl", "msg-a", model="claude-opus-5-1")
        state = cue.empty_state()
        _, imputed, _, _ = cue.update_state(state, self.d)
        self.assertEqual(imputed, {"claude-opus-5-1": "opus"})
        self.assertEqual({k.split("\x1f")[0] for k in state["cost"]},
                         {"claude-opus-5-1"})

    def test_qualified_variant_is_not_priced_at_the_base_rate(self):
        """A suffix that is not a dated snapshot must not resolve to the base
        model - guessing a qualified variant's rate risks undercounting."""
        for variant in ("claude-opus-5-fast", "claude-opus-5[1m]"):
            self.assertEqual(cue.normalize_model(variant), variant)
            self.assertEqual(cue.resolve_price(variant), (None, None))

    def test_render_emits_imputed_price_series(self):
        text = cue.render(cue.empty_state(), set(), {"claude-opus-9": "opus"},
                          1, 0.01, "")
        self.assertIn(
            'claude_code_imputed_price_model_info{model="claude-opus-9",tier="opus"} 1',
            text)
        self.assertIn("claude_code_usage_exporter_imputed_price_models 1", text)


if __name__ == "__main__":
    unittest.main()
