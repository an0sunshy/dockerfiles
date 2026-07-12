#!/usr/bin/env python3
"""Export Claude Code token/cost usage to Prometheus textfile format.

Parses local session transcripts (~/.claude/projects/**/*.jsonl) — which record
every assistant message's token usage for BOTH interactive and headless (`-p`)
sessions — dedups by (message.id, requestId), aggregates by model/type/entrypoint,
and writes a Prometheus .prom file for the node/unix-exporter textfile collector.

Why transcripts instead of OpenTelemetry: Claude Code's OTLP exporter does not
emit anything in headless/print mode (verified on v2.1.x), and ~89% of usage on
the automation host is headless. Transcripts capture all of it, plus full history.

Cost is the API-EQUIVALENT dollar value (list price), not a subscription bill —
on a Max/Pro plan it answers "what would this have cost on the API" for the
upgrade/value question. Cache writes are priced by actual TTL (5m vs 1h) from the
`ephemeral_*_input_tokens` split; cache reads at 0.1x input.

Durable monotonic counter: the emitted metrics are Prometheus `counter`s, so they
must never decrease. The transcript store on disk SHRINKS (sessions cleared with
`/clear`, pruned, rotated), so recomputing an absolute total from disk each run
drops when a transcript disappears — Prometheus reads that as a counter reset and
inflates increase()/rate(). Instead we accumulate incrementally: a persisted
`--state-file` holds per-series running totals plus the set of already-counted
message identities, so a deleted transcript never subtracts and a re-parse never
double-counts. Without --state-file the accumulator is in-memory only (monotonic
within a process, re-baselines on restart).

Scaling note: the persisted seen-set grows with total messages ever counted (one
8-byte hash each). ~257MB of transcripts parses in ~1.5s today; if the seen-set
grows past a few million entries, switch to a per-file (path, byte-offset) cursor
so appended lines are read without rehashing the whole store.
"""
import argparse
import glob
import hashlib
import http.server
import json
import os
import sys
import threading
import time
from collections import defaultdict

# Base list pricing, $ per 1M tokens (input, output).
# Source: claude-api skill model table (cached 2026-07-03). Cache rates derived
# from documented multipliers: read 0.1x, write-5m 1.25x, write-1h 2.0x of input.
PRICING = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-opus-4-7":  (5.0, 25.0),
    "claude-opus-4-6":  (5.0, 25.0),
    "claude-opus-4-5":  (5.0, 25.0),
    "claude-sonnet-5":  (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Self-hosted models (odin llm-dual LiteLLM router) — no API cost.
    "smart": (0.0, 0.0),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# Token buckets tracked per (model, entrypoint).
TYPES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")

# Bump when the on-disk state schema changes incompatibly.
STATE_VERSION = 1

# Separator joining (model, entrypoint) into a single JSON-safe series key. The
# ASCII unit separator never appears in a model id or entrypoint string.
_SEP = "\x1f"


def normalize_model(model):
    """Strip a dated snapshot suffix (e.g. claude-haiku-4-5-20251001)."""
    if not model:
        return "unknown"
    for known in PRICING:
        if model == known or model.startswith(known + "-"):
            return known
    return model


def _ident_hash(s):
    """Stable, compact identity for the seen-set (8-byte blake2b, 16 hex chars)."""
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"),
                           digest_size=8).hexdigest()


def _skey(model, entry):
    return f"{model}{_SEP}{entry}"


def empty_state():
    """A fresh, zeroed accumulator.

    tokens:   series-key -> {type: int}
    cost:     series-key -> usd float
    messages: series-key -> int
    seen:     set of counted message-identity hashes (dedup across runs/restarts)
    sessions: set of session-id hashes (distinct-session count)
    """
    return {
        "version": STATE_VERSION,
        "tokens": {},
        "cost": {},
        "messages": {},
        "seen": set(),
        "sessions": set(),
    }


def load_state(path):
    """Load persisted accumulator, or return an empty one if absent/corrupt.

    Corrupt/unreadable state degrades to empty (with a warning) rather than
    crash-looping; the next parse re-accumulates from whatever is on disk, which
    re-baselines the counter once — better than never serving metrics.
    """
    if not path or not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        st = empty_state()
        st["tokens"] = {k: {t: int(v.get(t, 0)) for t in TYPES}
                        for k, v in data["tokens"].items()}
        st["cost"] = {k: float(v) for k, v in data["cost"].items()}
        st["messages"] = {k: int(v) for k, v in data["messages"].items()}
        st["seen"] = set(data.get("seen", []))
        st["sessions"] = set(data.get("sessions", []))
        return st
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as e:
        print(f"warning: could not load state file {path} ({e}); starting fresh",
              file=sys.stderr)
        return empty_state()


def save_state(path, state):
    """Atomically persist the accumulator (tmp + os.replace, like the .prom write)."""
    data = {
        "version": STATE_VERSION,
        "tokens": state["tokens"],
        "cost": state["cost"],
        "messages": state["messages"],
        # list(), not sorted() — order is irrelevant on reload (rebuilt into a
        # set) and saves the O(n log n) sort under the serve lock as it grows.
        "seen": list(state["seen"]),
        "sessions": list(state["sessions"]),
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def persist_state(path, state):
    """Save state, but never let a persistence failure take metrics down.

    Symmetric with load_state's graceful degradation: if /state is unwritable
    (full disk, read-only remount), warn and keep serving from the in-memory
    accumulator rather than propagating OSError out of the scrape path.
    """
    try:
        save_state(path, state)
    except OSError as e:
        print(f"warning: could not persist state to {path} ({e}); "
              f"continuing from memory", file=sys.stderr)


def update_state(state, projects_dir):
    """Fold any not-yet-counted transcript messages into `state` (mutates it).

    Only NEW message identities are added — deletions never subtract, re-parses
    never double-count — so the accumulated totals are monotonic. Returns the set
    of unpriced models seen on disk this run (a gauge; not banked), the file
    count, and whether the accumulator changed (so callers skip a no-op save).
    """
    tokens = state["tokens"]
    cost = state["cost"]
    messages = state["messages"]
    seen = state["seen"]
    sessions = state["sessions"]
    baseline = len(seen) + len(sessions)  # to report whether anything new was folded in
    unpriced = set()  # models on disk absent from PRICING — deferred, not counted
    files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)

    for fp in files:
        try:
            rel = os.path.relpath(fp, projects_dir)
            with open(fp, "r") as f:
                for line_no, line in enumerate(f):
                    if '"usage"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    msg = o.get("message") or {}
                    u = msg.get("usage") or {}
                    if not u:
                        continue

                    mid, rid = msg.get("id"), o.get("requestId")
                    if mid is None and rid is None:
                        # Keyless record: identity from position in its append-only
                        # file, so repeated scrapes dedup it too.
                        ident = f"{rel}#{line_no}"
                    else:
                        ident = f"{mid}{_SEP}{rid}"
                    h = _ident_hash(ident)
                    if h in seen:
                        continue

                    model = normalize_model(msg.get("model"))
                    if model.startswith("<"):  # e.g. "<synthetic>" — no real usage
                        continue
                    sid = o.get("sessionId")
                    if sid:
                        sessions.add(_ident_hash(sid))
                    if model not in PRICING:
                        # Defer: don't bank at $0 (which would freeze this cost
                        # forever) and don't mark seen — once PRICING gains the
                        # model, a later run counts it while it's still on disk.
                        unpriced.add(model)
                        continue

                    seen.add(h)
                    entry = o.get("entrypoint") or "unknown"

                    inp = u.get("input_tokens", 0) or 0
                    out = u.get("output_tokens", 0) or 0
                    cread = u.get("cache_read_input_tokens", 0) or 0
                    cc = u.get("cache_creation") or {}
                    c1h = cc.get("ephemeral_1h_input_tokens")
                    c5m = cc.get("ephemeral_5m_input_tokens")
                    if c1h is None and c5m is None:
                        # No TTL split available — attribute all cache writes to 5m.
                        c5m = u.get("cache_creation_input_tokens", 0) or 0
                        c1h = 0
                    else:
                        c1h = c1h or 0
                        c5m = c5m or 0

                    k = _skey(model, entry)
                    td = tokens.setdefault(k, {t: 0 for t in TYPES})
                    td["input"] += inp
                    td["output"] += out
                    td["cache_read"] += cread
                    td["cache_write_5m"] += c5m
                    td["cache_write_1h"] += c1h
                    messages[k] = messages.get(k, 0) + 1

                    p_in, p_out = PRICING[model]
                    cost[k] = cost.get(k, 0.0) + (
                        inp * p_in
                        + cread * p_in * CACHE_READ_MULT
                        + c5m * p_in * CACHE_WRITE_5M_MULT
                        + c1h * p_in * CACHE_WRITE_1H_MULT
                        + out * p_out
                    ) / 1_000_000.0
        except OSError:
            continue

    dirty = (len(seen) + len(sessions)) != baseline  # only-add, so != means grew
    return unpriced, len(files), dirty


def render(state, unpriced, n_files, duration, host):
    """Render Prometheus text-exposition format from the accumulated state.

    On hosts running alloy-remote, `host` is added by remote_write external_labels,
    so leave --host empty there. Set --host only where nothing else labels the series.
    """
    def lp(name, value):  # one label pair, with Prometheus value escaping
        v = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'{name}="{v}"'

    host_pair = lp("host", host) if host else ""

    def labels(*pairs):
        joined = ",".join(p for p in ((host_pair,) + pairs) if p)
        return "{" + joined + "}" if joined else ""

    out = []

    def family(name, helptext, mtype, rows):
        """Emit one metric family: HELP/TYPE header + a line per (label_pairs, value)."""
        out.append(f"# HELP {name} {helptext}")
        out.append(f"# TYPE {name} {mtype}")
        for label_pairs, value in rows:
            out.append(f"{name}{labels(*label_pairs)} {value}")

    tokens, cost, messages = state["tokens"], state["cost"], state["messages"]

    tok_rows = []
    for k, d in sorted(tokens.items()):
        model, entry = k.split(_SEP, 1)
        for t in TYPES:
            tok_rows.append(((lp("model", model), lp("type", t),
                              lp("entrypoint", entry)), d.get(t, 0)))
    family("claude_code_tokens_total",
           "Cumulative Claude Code token usage (parsed from local transcripts).",
           "counter", tok_rows)

    cost_rows = []
    for k, c in sorted(cost.items()):
        model, entry = k.split(_SEP, 1)
        cost_rows.append(((lp("model", model), lp("entrypoint", entry)), f"{c:.6f}"))
    family("claude_code_cost_usd_total",
           "Cumulative API-equivalent cost in USD (list price; not a subscription bill).",
           "counter", cost_rows)

    msg_rows = []
    for k, m in sorted(messages.items()):
        model, entry = k.split(_SEP, 1)
        msg_rows.append(((lp("model", model), lp("entrypoint", entry)), m))
    family("claude_code_messages_total",
           "Cumulative assistant message count.", "counter", msg_rows)

    # Info series (value 1) naming each model seen on disk but missing from
    # PRICING — its usage is deferred (uncounted) until priced. Normally emits no
    # series; when one appears, alerting fires on the scalar gauge below and reads
    # the model name here.
    family("claude_code_unpriced_model_info",
           "Models seen in transcripts but absent from the PRICING table (value 1; usage deferred until priced).",
           "gauge", (((lp("model", model),), 1) for model in sorted(unpriced)))

    ts = int(time.time())
    for name, helptext, mtype, value in (
        ("claude_code_sessions_total", "Distinct Claude Code session count.", "counter", len(state["sessions"])),
        ("claude_code_usage_exporter_unpriced_models", "Count of distinct models seen but absent from the PRICING table (usage deferred).", "gauge", len(unpriced)),
        ("claude_code_usage_exporter_transcripts", "Transcript files parsed in the last run.", "gauge", n_files),
        ("claude_code_usage_exporter_last_run_timestamp_seconds", "Unix time of the last exporter run.", "gauge", ts),
        ("claude_code_usage_exporter_duration_seconds", "Wall-clock seconds of the last exporter run.", "gauge", round(duration, 3)),
    ):
        family(name, helptext, mtype, [((), value)])

    return "\n".join(out) + "\n"


def human_summary(state):
    by_model_cost = defaultdict(float)
    by_entry_cost = defaultdict(float)
    grand_tokens = defaultdict(int)
    for k, c in state["cost"].items():
        model, entry = k.split(_SEP, 1)
        by_model_cost[model] += c
        by_entry_cost[entry] += c
    for k, d in state["tokens"].items():
        for t in TYPES:
            grand_tokens[t] += d.get(t, 0)
    lines = [f"sessions: {len(state['sessions'])}   messages: {sum(state['messages'].values())}", ""]
    lines.append("API-equivalent $ by model:")
    for m, c in sorted(by_model_cost.items(), key=lambda x: -x[1]):
        lines.append(f"  {m:<26} ${c:,.2f}")
    lines.append("\nAPI-equivalent $ by entrypoint:")
    for e, c in sorted(by_entry_cost.items(), key=lambda x: -x[1]):
        lines.append(f"  {e:<26} ${c:,.2f}")
    lines.append(f"\nTOTAL API-equivalent value: ${sum(by_model_cost.values()):,.2f}")
    gt = sum(grand_tokens.values())
    lines.append(f"gross tokens: {gt:,}   output: {grand_tokens['output']:,}   cache_read: {grand_tokens['cache_read']:,}")
    return "\n".join(lines)


def serve(args):
    """Run an HTTP server exposing /metrics, scraped by Prometheus/Alloy.

    The scrape interval replaces the cron entirely. Re-parses at most every
    --cache-ttl seconds (so a fast scrape cadence doesn't re-read the transcripts
    each time). No host label is emitted — the scraper's remote_write adds it.

    State is loaded once at startup and persisted after each parse, so the
    accumulated counters survive a restart when --state-file is set.
    """
    state = load_state(args.state_file)
    rendered = {"text": "", "ts": None}
    lock = threading.Lock()

    def regen():
        start = time.time()
        unpriced, n_files, dirty = update_state(state, args.projects_dir)
        if unpriced:
            print(f"warning: model(s) absent from pricing table, usage deferred: "
                  f"{', '.join(sorted(unpriced))}", file=sys.stderr)
        if args.state_file and dirty:
            persist_state(args.state_file, state)
        rendered["text"] = render(state, unpriced, n_files,
                                  time.time() - start, args.host)
        rendered["ts"] = time.monotonic()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/healthz"):
                return self._send(200, b"claude-usage-exporter ok\n", "text/plain")
            if path != "/metrics":
                return self._send(404, b"not found\n", "text/plain")
            with lock:
                if rendered["ts"] is None or time.monotonic() - rendered["ts"] > args.cache_ttl:
                    regen()
                body = rendered["text"].encode()
            self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # one line per scrape is just noise

    httpd = http.server.ThreadingHTTPServer((args.listen, args.port), Handler)
    print(f"claude-usage-exporter serving /metrics on {args.listen}:{args.port} "
          f"(re-parse cached {args.cache_ttl}s, state={args.state_file or 'in-memory'})",
          file=sys.stderr)
    httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--output", help="Path to write the .prom file (omit with --print/--serve).")
    ap.add_argument("--host", default="", help="Optional host label (set on hosts without alloy external_labels, e.g. Mac).")
    ap.add_argument("--state-file", default="", help="Path to the persisted accumulator (JSON). Required for a durable monotonic counter across restarts; omit for in-memory only.")
    ap.add_argument("--print", action="store_true", dest="do_print", help="Print a human summary to stdout instead of writing .prom (read-only; never persists state).")
    ap.add_argument("--serve", action="store_true", help="Run as an HTTP /metrics server for Prometheus/Alloy to scrape (no cron needed).")
    ap.add_argument("--port", type=int, default=9119, help="Port for --serve (default 9119).")
    ap.add_argument("--listen", default="127.0.0.1", help="Bind address for --serve (default 127.0.0.1 loopback; only a local scraper reaches it).")
    ap.add_argument("--cache-ttl", type=int, default=60, help="Max age (s) of the cached parse in --serve mode (default 60).")
    args = ap.parse_args()

    if args.serve:
        serve(args)
        return

    state = load_state(args.state_file)
    start = time.time()
    unpriced, n_files, dirty = update_state(state, args.projects_dir)
    duration = time.time() - start

    if unpriced:
        print(f"warning: {len(unpriced)} model(s) absent from pricing table, usage deferred: "
              f"{', '.join(sorted(unpriced))} — add them to PRICING.", file=sys.stderr)

    if args.do_print:
        # Read-only view: reflect the latest disk, but never persist from --print
        # (it may run via `docker exec` alongside a serving process sharing state).
        print(human_summary(state))
        print(f"\nparsed {n_files} transcripts in {duration:.2f}s")
        return

    if not args.output:
        ap.error("--output is required unless --print is given")

    if args.state_file and dirty:
        persist_state(args.state_file, state)

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    text = render(state, unpriced, n_files, duration, args.host)
    tmp = f"{args.output}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, args.output)  # atomic — collector never sees a partial file


if __name__ == "__main__":
    main()
