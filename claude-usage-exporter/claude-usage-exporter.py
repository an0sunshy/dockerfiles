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

Scaling note: this re-parses every transcript each run (global dedup requires it).
~257MB parses in ~1.5s today; if the transcript store grows past a few GB, switch
to an incremental file-mtime cache with a persisted global seen-set.
"""
import argparse
import glob
import http.server
import json
import os
import sys
import threading
import time
from collections import defaultdict

# Base list pricing, $ per 1M tokens (input, output).
# Source: claude-api skill model table (cached 2026-05-26). Cache rates derived
# from documented multipliers: read 0.1x, write-5m 1.25x, write-1h 2.0x of input.
PRICING = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-opus-4-7":  (5.0, 25.0),
    "claude-opus-4-6":  (5.0, 25.0),
    "claude-opus-4-5":  (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# Token buckets tracked per (model, entrypoint).
TYPES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")


def normalize_model(model):
    """Strip a dated snapshot suffix (e.g. claude-haiku-4-5-20251001)."""
    if not model:
        return "unknown"
    for known in PRICING:
        if model == known or model.startswith(known + "-"):
            return known
    return model


def parse(projects_dir):
    tokens = defaultdict(lambda: defaultdict(int))   # (model, entrypoint) -> type -> tokens
    cost = defaultdict(float)                          # (model, entrypoint) -> usd
    messages = defaultdict(int)                        # (model, entrypoint) -> count
    sessions = set()
    seen = set()
    unpriced = set()  # models seen in transcripts but absent from PRICING (counted at $0)
    files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)

    for fp in files:
        try:
            with open(fp, "r") as f:
                for line in f:
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
                    key = (msg.get("id"), o.get("requestId"))
                    if key != (None, None):  # only dedup messages that actually carry an id
                        if key in seen:
                            continue
                        seen.add(key)

                    model = normalize_model(msg.get("model"))
                    if model.startswith("<"):  # e.g. "<synthetic>" — no real usage
                        continue
                    if model not in PRICING:
                        unpriced.add(model)
                    entry = o.get("entrypoint") or "unknown"
                    if o.get("sessionId"):
                        sessions.add(o["sessionId"])

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

                    k = (model, entry)
                    tokens[k]["input"] += inp
                    tokens[k]["output"] += out
                    tokens[k]["cache_read"] += cread
                    tokens[k]["cache_write_5m"] += c5m
                    tokens[k]["cache_write_1h"] += c1h
                    messages[k] += 1

                    p_in, p_out = PRICING.get(model, (0.0, 0.0))
                    cost[k] += (
                        inp * p_in
                        + cread * p_in * CACHE_READ_MULT
                        + c5m * p_in * CACHE_WRITE_5M_MULT
                        + c1h * p_in * CACHE_WRITE_1H_MULT
                        + out * p_out
                    ) / 1_000_000.0
        except OSError:
            continue

    return tokens, cost, messages, sessions, unpriced, len(files)


def render(tokens, cost, messages, sessions, unpriced, n_files, duration, host):
    """Render Prometheus text-exposition format.

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

    family("claude_code_tokens_total",
           "Cumulative Claude Code token usage (parsed from local transcripts).", "counter",
           (((lp("model", model), lp("type", t), lp("entrypoint", entry)), d[t])
            for (model, entry), d in sorted(tokens.items()) for t in TYPES))
    family("claude_code_cost_usd_total",
           "Cumulative API-equivalent cost in USD (list price; not a subscription bill).", "counter",
           (((lp("model", model), lp("entrypoint", entry)), f"{c:.6f}")
            for (model, entry), c in sorted(cost.items())))
    family("claude_code_messages_total",
           "Cumulative assistant message count.", "counter",
           (((lp("model", model), lp("entrypoint", entry)), m)
            for (model, entry), m in sorted(messages.items())))

    # Info series (value 1) naming each model seen in transcripts but missing from
    # PRICING — its usage is counted at $0. Normally emits no series; when one
    # appears, alerting fires on the scalar gauge below and reads the model name here.
    family("claude_code_unpriced_model_info",
           "Models seen in transcripts but absent from the PRICING table (value 1; usage counted at $0 until added).", "gauge",
           (((lp("model", model),), 1) for model in sorted(unpriced)))

    ts = int(time.time())
    for name, helptext, mtype, value in (
        ("claude_code_sessions_total", "Distinct Claude Code session count.", "counter", len(sessions)),
        ("claude_code_usage_exporter_unpriced_models", "Count of distinct models seen but absent from the PRICING table (usage counted at $0).", "gauge", len(unpriced)),
        ("claude_code_usage_exporter_transcripts", "Transcript files parsed in the last run.", "gauge", n_files),
        ("claude_code_usage_exporter_last_run_timestamp_seconds", "Unix time of the last exporter run.", "gauge", ts),
        ("claude_code_usage_exporter_duration_seconds", "Wall-clock seconds of the last exporter run.", "gauge", round(duration, 3)),
    ):
        family(name, helptext, mtype, [((), value)])

    return "\n".join(out) + "\n"


def human_summary(tokens, cost, messages, sessions):
    by_model_cost = defaultdict(float)
    by_entry_cost = defaultdict(float)
    grand_tokens = defaultdict(int)
    for (model, entry), c in cost.items():
        by_model_cost[model] += c
        by_entry_cost[entry] += c
    for (model, entry), d in tokens.items():
        for t in TYPES:
            grand_tokens[t] += d[t]
    lines = [f"sessions: {len(sessions)}   messages: {sum(messages.values())}", ""]
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
    """
    state = {"text": "", "ts": None}
    lock = threading.Lock()

    def regen():
        start = time.time()
        tokens, cost, messages, sessions, unpriced, n_files = parse(args.projects_dir)
        if unpriced:
            print(f"warning: model(s) absent from pricing table, counted at $0: "
                  f"{', '.join(sorted(unpriced))}", file=sys.stderr)
        state["text"] = render(tokens, cost, messages, sessions, unpriced, n_files,
                               time.time() - start, args.host)
        state["ts"] = time.monotonic()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/healthz"):
                return self._send(200, b"claude-usage-exporter ok\n", "text/plain")
            if path != "/metrics":
                return self._send(404, b"not found\n", "text/plain")
            with lock:
                if state["ts"] is None or time.monotonic() - state["ts"] > args.cache_ttl:
                    regen()
                body = state["text"].encode()
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
          f"(re-parse cached {args.cache_ttl}s)", file=sys.stderr)
    httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects-dir", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--output", help="Path to write the .prom file (omit with --print/--serve).")
    ap.add_argument("--host", default="", help="Optional host label (set on hosts without alloy external_labels, e.g. Mac).")
    ap.add_argument("--print", action="store_true", dest="do_print", help="Print a human summary to stdout instead of writing .prom.")
    ap.add_argument("--serve", action="store_true", help="Run as an HTTP /metrics server for Prometheus/Alloy to scrape (no cron needed).")
    ap.add_argument("--port", type=int, default=9119, help="Port for --serve (default 9119).")
    ap.add_argument("--listen", default="127.0.0.1", help="Bind address for --serve (default 127.0.0.1 loopback; only a local scraper reaches it).")
    ap.add_argument("--cache-ttl", type=int, default=60, help="Max age (s) of the cached parse in --serve mode (default 60).")
    args = ap.parse_args()

    if args.serve:
        serve(args)
        return

    start = time.time()
    tokens, cost, messages, sessions, unpriced, n_files = parse(args.projects_dir)
    duration = time.time() - start

    if unpriced:
        print(f"warning: {len(unpriced)} model(s) absent from pricing table, counted at $0: "
              f"{', '.join(sorted(unpriced))} — add them to PRICING.", file=sys.stderr)

    if args.do_print:
        print(human_summary(tokens, cost, messages, sessions))
        print(f"\nparsed {n_files} transcripts in {duration:.2f}s")
        return

    if not args.output:
        ap.error("--output is required unless --print is given")

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    text = render(tokens, cost, messages, sessions, unpriced, n_files, duration, args.host)
    tmp = f"{args.output}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, args.output)  # atomic — collector never sees a partial file


if __name__ == "__main__":
    main()
