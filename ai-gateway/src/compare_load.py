"""The load comparison (step 10 stage 3): the same k6 load (compare/load.js) at the gateway, at LiteLLM's proxy, and
at the fake upstream directly, the floor both gateways add to. Free: nothing leaves this PC.

    docker compose --profile compare up -d --build --wait
    uv run python src/compare_load.py                    # about 15 minutes; writes results/compare/load.{json,md}
    uv run python src/compare_load.py --rates 25 --hold 10s       # a quick look

Plain language: the same stream of orders at three counters: the kitchen's own hatch (no waiter at all), and each of the
two waiters. What a waiter adds over the hatch is its cost: time per order, and how much of its one CPU it burns.

For each target and rate: latency (median, p95, p99) and time to first byte, how many requests failed, whether k6
could keep the rate (dropped iterations mean it ran out of virtual users: the target fell behind), and the containers'
peak CPU and memory from `docker stats` sampled during the run. Each gateway has one CPU and one process
(docker-compose.yml), and one key: the gateway's has its rate limits raised out of the way, so every request still
goes through admission and the ledger, as in production.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time

from compare_suite import GATEWAY_URL, LITELLM_URL, UPSTREAM_URL, GatewayAdmin, LiteLLMAdmin
from paths import RESULTS_DIR, ROOT

OUT_DIR = RESULTS_DIR / "compare"
SCRIPT = ROOT / "compare" / "load.js"
CONTAINERS = {"gateway": "ai-gateway-gateway-1", "litellm": "ai-gateway-litellm-1",
              "upstream": "ai-gateway-fake-upstream-1", "db": "ai-gateway-db-1"}  # fmt: skip
RATES = [10, 20, 30, 40, 60]
STREAM_RATE = 20
WARMUP_RATE, WARMUP_HOLD = 10, "15s"


def k6_path() -> str:
    local = pathlib.Path.home() / ".local" / "bin" / ("k6.exe" if sys.platform == "win32" else "k6")
    return str(local) if local.exists() else "k6"


# --- docker stats, sampled in the background during a run ---


def parse_cpu(text: str) -> float:
    return float(text.strip().rstrip("%") or 0)


def parse_mem_mib(text: str) -> float:
    """'587.9MiB / 1GiB' -> 587.9"""
    value, unit = re.match(r"([\d.]+)\s*([KMG]i?B)", text.strip()).groups()
    return round(float(value) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "KB": 1 / 1024, "MB": 1, "GB": 1024}[unit], 1)


class Sampler(threading.Thread):
    """Peak CPU (% of one core) and memory per container while a run lasts."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peaks: dict[str, dict] = {}
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
                capture_output=True, text=True,
            ).stdout  # fmt: skip
            for line in out.splitlines():
                name, cpu, mem = line.split("|")
                for label, container in CONTAINERS.items():
                    if name == container:
                        peak = self.peaks.setdefault(label, {"cpu_pct": 0.0, "mem_mib": 0.0})
                        peak["cpu_pct"] = max(peak["cpu_pct"], parse_cpu(cpu))
                        peak["mem_mib"] = max(peak["mem_mib"], parse_mem_mib(mem))

    def stop(self) -> dict:
        self.stop_flag.set()
        self.join(timeout=10)
        return self.peaks


# --- one k6 run ---


def summarize(summary: dict) -> dict:
    """The numbers that matter from k6's --summary-export."""
    m = summary["metrics"]
    duration, waiting = m["http_req_duration"], m["http_req_waiting"]
    return {
        "requests": int(m["http_reqs"]["count"]),
        "per_second": round(m["http_reqs"]["rate"], 1),
        "failed_pct": round(100 * m["http_req_failed"]["value"], 2),
        "dropped": int(m.get("dropped_iterations", {}).get("count", 0)),
        "p50_ms": round(duration["med"], 1),
        "p95_ms": round(duration["p(95)"], 1),
        "p99_ms": round(duration["p(99)"], 1),
        "max_ms": round(duration["max"], 1),
        "ttfb_p50_ms": round(waiting["med"], 1),
        "ttfb_p95_ms": round(waiting["p(95)"], 1),
    }


def k6_run(url: str, key: str, model: str, rate: int, hold: str, stream: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        export = pathlib.Path(tmp) / "summary.json"
        env = os.environ | {"COMPARE_KEY": key}  # the key goes in the environment, never on the command line
        args = [k6_path(), "run", "--quiet", "--no-color", f"--summary-export={export}", "-e", f"BASE_URL={url}",
                "-e", f"MODEL={model}", "-e", f"RATE={rate}", "-e", f"HOLD={hold}", "-e", f"STREAM={int(stream)}",
                str(SCRIPT)]  # fmt: skip
        subprocess.run(args, env=env, capture_output=True, text=True)
        return summarize(json.loads(export.read_text(encoding="utf-8")))


def targets() -> dict:
    gateway_admin = GatewayAdmin()
    key = gateway_admin.issue("load")
    gateway_admin.conn.execute(
        "update api_keys set requests_per_minute = 1000000, tokens_per_minute = 1000000000 where prefix = %s",
        (key[:10],),
    )
    import asyncio

    lite = LiteLLMAdmin()
    lite_key = (
        asyncio.run(lite.issue("load"), loop_factory=asyncio.SelectorEventLoop)
        if sys.platform == "win32"
        else asyncio.run(lite.issue("load"))
    )
    return {
        "upstream": (UPSTREAM_URL, "none", "fake-ok"),
        "gateway": (GATEWAY_URL, key, "chat"),
        "litellm": (LITELLM_URL, lite_key, "chat"),
    }


def measure(rates: list[int], hold: str, stream_rate: int | None) -> dict:
    runs = []
    plan = [(rate, False) for rate in rates] + ([(stream_rate, True)] if stream_rate else [])
    for name, (url, key, model) in targets().items():
        k6_run(url, key, model, WARMUP_RATE, WARMUP_HOLD, False)  # not recorded: first requests load code and pools
        for rate, stream in plan:
            sampler = Sampler()
            sampler.start()
            result = k6_run(url, key, model, rate, hold, stream)
            peaks = sampler.stop()
            row = {"target": name, "rate": rate, "stream": stream, **result,
                   "cpu_pct": peaks.get(name, {}).get("cpu_pct"), "mem_mib": peaks.get(name, {}).get("mem_mib"),
                   "db_cpu_pct": peaks.get("db", {}).get("cpu_pct")}  # fmt: skip
            runs.append(row)
            print(f"{name:<8} {rate:>4}/s {'stream' if stream else 'plain ':<6} p50 {row['p50_ms']:>7} ms  p95 "
                  f"{row['p95_ms']:>7} ms  failed {row['failed_pct']:>5}%  dropped {row['dropped']:>4}  "
                  f"cpu {row['cpu_pct']}%  mem {row['mem_mib']} MiB")  # fmt: skip
            time.sleep(5)
    return {"date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "hold": hold, "runs": runs}


def overhead(runs: list[dict]) -> list[dict]:
    """Each gateway's median and p95 minus the upstream's at the same rate and mode: what the gateway itself adds."""
    floor = {(r["rate"], r["stream"]): r for r in runs if r["target"] == "upstream"}
    out = []
    for r in runs:
        base = floor.get((r["rate"], r["stream"]))
        if r["target"] != "upstream" and base:
            out.append({"target": r["target"], "rate": r["rate"], "stream": r["stream"],
                        "added_p50_ms": round(r["p50_ms"] - base["p50_ms"], 1),
                        "added_p95_ms": round(r["p95_ms"] - base["p95_ms"], 1)})  # fmt: skip
    return out


def report(result: dict) -> str:
    lines = [
        "# Load: the gateway and LiteLLM's proxy, same load",
        "",
        f"Generated by `src/compare_load.py` on {result['date'][:10]}: k6 (compare/load.js) at a fixed rate for "
        f"{result['hold']} per row, against each gateway (one CPU, one process each) and the fake upstream directly. "
        "The fake upstream answers 20 words: the first after 100 ms, then one every 5 ms. CPU is the container's peak, "
        "in % of one core. See the README, step 10, for what it means.",
        "",
        "| Target | Rate/s | Mode | Served/s | p50 ms | p95 ms | p99 ms | First byte p50 ms | Failed % | Dropped | "
        "Peak CPU % | Peak MiB |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    columns = ("per_second", "p50_ms", "p95_ms", "p99_ms", "ttfb_p50_ms", "failed_pct", "dropped", "cpu_pct", "mem_mib")
    for r in result["runs"]:
        mode = "stream" if r["stream"] else "plain"
        lines.append(f"| {r['target']} | {r['rate']} | {mode} | " + " | ".join(str(r[c]) for c in columns) + " |")
    lines += ["", "**Added over the upstream** (the same rate and mode):", "",
              "| Target | Rate/s | Mode | Added p50 ms | Added p95 ms |", "|---|---|---|---|---|"]  # fmt: skip
    for o in result["overhead"]:
        mode = "stream" if o["stream"] else "plain"
        lines.append(f"| {o['target']} | {o['rate']} | {mode} | {o['added_p50_ms']} | {o['added_p95_ms']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rates", type=int, nargs="+", default=RATES)
    parser.add_argument("--hold", default="40s")
    parser.add_argument("--stream-rate", type=int, default=STREAM_RATE, help="0: no streamed run")
    args = parser.parse_args()
    result = measure(args.rates, args.hold, args.stream_rate or None)
    result["overhead"] = overhead(result["runs"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "load.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    (OUT_DIR / "load.md").write_text(report(result), encoding="utf-8", newline="\n")
    print("wrote results/compare/load.md")


if __name__ == "__main__":
    main()
