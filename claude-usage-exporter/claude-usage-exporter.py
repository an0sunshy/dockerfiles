#!/usr/bin/env python3
"""Export Claude Code token usage to Prometheus textfile format.

Parses local session transcripts (~/.claude/projects/**/*.jsonl) — which record
every assistant message's token usage for BOTH interactive and headless (`-p`)
sessions — dedups by (message.id, requestId), aggregates by model/type/entrypoint,
and writes a Prometheus .prom file for the node/unix-exporter textfile collector.

Why transcripts instead of OpenTelemetry: Claude Code's OTLP exporter does not
emit anything in headless/print mode (verified on v2.1.x), and ~89% of usage on
the automation host is headless. Transcripts capture all of it, plus full history.

THIS EXPORTER DOES NOT PRICE ANYTHING. It reports model names and token counts;
dollars are computed server-side by the `ai:anthropic_price_usd_per_mtok` and
`ai:local_price_usd_per_mtok` recording rules in the ansible-scripts repo
(docker-composes/misaka/loki/config/prometheus/ai-usage-rules.yml), which are the
single price registry.

That split is deliberate. A price table baked into this image made every new
model release a fleet-wide image rollout: the table shipped to each host, so a
host that missed a rebuild under-counted silently. neb ran a 3-week-old build
through the claude-opus-5 launch for exactly that reason. Prices now change in
one file on the monitoring host and apply to every host's history at once,
retroactively, because cost is derived from the token counters at query time
rather than banked at parse time.

The corollary is that EVERY model's tokens must be banked unconditionally, even
one this code has never heard of — an unknown model is the normal case now, not
an error. Anything dropped here can never be priced later.

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
import re
import sys
import threading
import time
from collections import defaultdict

# Legacy lane aliases served by the odin LiteLLM router, mapped to the official
# OpenRouter model id. New local lanes should use the official id directly
# (base model name, quantization/variant suffixes trimmed) so no alias is needed.
#
# This is model-NAME normalization, not pricing: it exists so one lane's usage
# lands on one series. The server's price rules key on the resulting id, so a
# name that arrives here un-aliased is what shows up as unpriced downstream.
LOCAL_MODEL_ALIASES = {
    "smart": "qwen/qwen3.6-35b-a3b",
}

# A dated snapshot suffix, e.g. the -20251001 of claude-haiku-4-5-20251001. This
# is the ONLY suffix normalize_model strips, so every snapshot of a release
# aggregates into one series while a dot-release (claude-opus-5-1) stays distinct
# from its parent - they may not share a price, and the server prices per id.
_SNAPSHOT_RE = re.compile(r"-\d{8}$")

# Token buckets tracked per (model, entrypoint).
TYPES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")

# Bump when the on-disk state schema changes incompatibly.
STATE_VERSION = 1

# Separator joining (model, entrypoint) into a single JSON-safe series key. The
# ASCII unit separator never appears in a model id or entrypoint string.
_SEP = "\x1f"


def normalize_model(model):
    """Strip a dated snapshot suffix (e.g. claude-haiku-4-5-20251001).

    A dated snapshot is the ONLY thing stripped, so every snapshot of a release
    aggregates into one series instead of fanning out per date, while a
    dot-release or qualified variant stays its own model rather than being
    folded into a parent it may not share a price with.
    """
    if not model:
        return "unknown"
    model = LOCAL_MODEL_ALIASES.get(model, model)
    return _SNAPSHOT_RE.sub("", model)


def _ident_hash(s):
    """Stable, compact identity for the seen-set (8-byte blake2b, 16 hex chars)."""
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"),
                           digest_size=8).hexdigest()


def _skey(model, entry):
    return f"{model}{_SEP}{entry}"


def empty_state():
    """A fresh, zeroed accumulator.

    tokens:   series-key -> {type: int}
    messages: series-key -> int
    seen:     set of counted message-identity hashes (dedup across runs/restarts)
    sessions: set of session-id hashes (distinct-session count)
    """
    return {
        "version": STATE_VERSION,
        "tokens": {},
        "messages": {},
        "seen": set(),
        "sessions": set(),
    }


def load_state(path):
    """Load persisted accumulator, or return an empty one if absent/corrupt.

    Corrupt/unreadable state degrades to empty (with a warning) rather than
    crash-looping; the next parse re-accumulates from whatever is on disk, which
    re-baselines the counter once — better than never serving metrics.

    A state file written by a pricing-era build carries an extra "cost" key; it
    is ignored rather than rejected, so the upgrade keeps the seen-set and the
    token counters continue unbroken instead of re-baselining. Rolling BACK to
    such a build is the lossy direction: its loader requires "cost", so it takes
    the corrupt path above and re-accumulates from whatever transcripts remain.
    """
    if not path or not os.path.exists(path):
        return empty_state()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        st = empty_state()
        st["tokens"] = {k: {t: int(v.get(t, 0)) for t in TYPES}
                        for k, v in data["tokens"].items()}
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
    never double-count — so the accumulated totals are monotonic. Returns the
    file count and whether the accumulator changed (so callers skip a no-op save).

    Every model is banked, including ids this code has never seen. Pricing is the
    server's job, and a token dropped here is unrecoverable once the transcript
    ages out; an unknown id costs one extra series and prices retroactively the
    moment a rule names it.
    """
    tokens = state["tokens"]
    messages = state["messages"]
    seen = state["seen"]
    sessions = state["sessions"]
    baseline = len(seen) + len(sessions)  # to report whether anything new was folded in
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
        except OSError:
            continue

    dirty = (len(seen) + len(sessions)) != baseline  # only-add, so != means grew
    return len(files), dirty


def render(state, n_files, duration, host):
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

    tokens, messages = state["tokens"], state["messages"]

    tok_rows = []
    for k, d in sorted(tokens.items()):
        model, entry = k.split(_SEP, 1)
        for t in TYPES:
            tok_rows.append(((lp("model", model), lp("type", t),
                              lp("entrypoint", entry)), d.get(t, 0)))
    family("claude_code_tokens_total",
           "Cumulative Claude Code token usage (parsed from local transcripts).",
           "counter", tok_rows)

    msg_rows = []
    for k, m in sorted(messages.items()):
        model, entry = k.split(_SEP, 1)
        msg_rows.append(((lp("model", model), lp("entrypoint", entry)), m))
    family("claude_code_messages_total",
           "Cumulative assistant message count.", "counter", msg_rows)

    # No unpriced/imputed gauges: this build holds no prices, so it cannot know
    # whether a model is priced. The equivalent signal is server-side —
    # AiUsageUnpricedModel fires on tokens arriving with no matching price rule,
    # which covers every lane at once rather than only what a client could see.

    ts = int(time.time())
    for name, helptext, mtype, value in (
        ("claude_code_sessions_total", "Distinct Claude Code session count.", "counter", len(state["sessions"])),
        ("claude_code_usage_exporter_transcripts", "Transcript files parsed in the last run.", "gauge", n_files),
        ("claude_code_usage_exporter_last_run_timestamp_seconds", "Unix time of the last exporter run.", "gauge", ts),
        ("claude_code_usage_exporter_duration_seconds", "Wall-clock seconds of the last exporter run.", "gauge", round(duration, 3)),
    ):
        family(name, helptext, mtype, [((), value)])

    return "\n".join(out) + "\n"


def human_summary(state):
    """Token breakdown for --print.

    Reports tokens, not dollars: this build carries no price table, and inventing
    one here purely for the terminal would recreate the second registry the split
    exists to remove. Dollars live on the AI Usage dashboard, which prices the
    same counters server-side.
    """
    by_model = defaultdict(int)
    by_entry = defaultdict(int)
    grand_tokens = defaultdict(int)
    for k, d in state["tokens"].items():
        model, entry = k.split(_SEP, 1)
        n = sum(d.get(t, 0) for t in TYPES)
        by_model[model] += n
        by_entry[entry] += n
        for t in TYPES:
            grand_tokens[t] += d.get(t, 0)
    lines = [f"sessions: {len(state['sessions'])}   messages: {sum(state['messages'].values())}", ""]
    lines.append("tokens by model:")
    for m, n in sorted(by_model.items(), key=lambda x: -x[1]):
        lines.append(f"  {m:<26} {n:>15,}")
    lines.append("\ntokens by entrypoint:")
    for e, n in sorted(by_entry.items(), key=lambda x: -x[1]):
        lines.append(f"  {e:<26} {n:>15,}")
    gt = sum(grand_tokens.values())
    lines.append(f"\nTOTAL tokens: {gt:,}")
    lines.append(f"input: {grand_tokens['input']:,}   output: {grand_tokens['output']:,}   "
                 f"cache_read: {grand_tokens['cache_read']:,}   "
                 f"cache_write: {grand_tokens['cache_write_5m'] + grand_tokens['cache_write_1h']:,}")
    lines.append("\n$ figures: see the AI Usage dashboard (prices live server-side).")
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
        n_files, dirty = update_state(state, args.projects_dir)
        if args.state_file and dirty:
            persist_state(args.state_file, state)
        rendered["text"] = render(state, n_files, time.time() - start, args.host)
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
    n_files, dirty = update_state(state, args.projects_dir)
    duration = time.time() - start

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
    text = render(state, n_files, duration, args.host)
    tmp = f"{args.output}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, args.output)  # atomic — collector never sees a partial file


if __name__ == "__main__":
    main()
