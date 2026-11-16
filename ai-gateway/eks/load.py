"""Step 8's load tests again, on EKS (step 11 stage 3), with k6 running inside the cluster as a Job.

    uv run python eks/load.py autoscale     # load/chat.js's profile: 10 -> 60 requests a second, held, back to 10
    uv run python eks/load.py rollout       # 12 a second for 4 minutes, with two rolling updates of the gateway

Writes results/eks/<test>.{json,md}. Free apart from the cluster's own hourly cost: the `stub` route answers offline.

Plain language: the same exam step 8 set on the practice cluster, now on the rented one. The test traffic starts
inside the cluster, next to the gateway, so the numbers are the cluster's, not this PC's internet connection.

How a run goes:
1. 20 teams allowed only `stub`, and a key each, made in one `kubectl exec` of a gateway pod (step 8 found 40 separate
   execs were load enough to scale the gateway). The keys come back to this script, never printed or logged, and
   reach k6 through a Secret.
2. A wait until the autoscaler is back at 2 pods, so every run starts the same way.
3. k6 (the image of the version installed here, pinned by digest) runs load/chat.js unchanged, through a small wrapper
   that prints its summary as one JSON line. Every 5 s this script records the autoscaler's pods and CPU, the ready
   pods and the nodes.
4. The keys are revoked and the Secret, ConfigMap and Job deleted, whatever happened.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time

from up import AI_GATEWAY, ensure_context, run, tool

OUT = AI_GATEWAY / "results" / "eks"
K6_IMAGE = "grafana/k6:2.3.0@sha256:2dd4756ec6e14e62778916e1eb850108e4b499304c02df56760fa0bdb0fba0b8"
TEAMS = 20
GATEWAY = "deploy/ai-gateway-gateway"
WRAPPER = """// load/chat.js unchanged, with its summary printed as one JSON line for eks/load.py to read
export { options, setup, default } from "./chat.js";
export function handleSummary(data) {
  return { stdout: "K6_SUMMARY " + JSON.stringify(data) + "\\n" };
}
"""
TESTS = {
    "autoscale": {"env": {"PEAK": "60"}, "restarts_at": []},
    "rollout": {"env": {"RATE": "12", "HOLD": "4m"}, "restarts_at": [60, 150]},
}


def kubectl(*args: str, input: str | None = None, capture: bool = True, check: bool = True) -> str:
    return run([tool("kubectl"), *args], input=input, capture=capture, check=check)


def in_gateway(code: str) -> str:
    """Python run in a gateway pod, from its src/ (where admin.py and the database settings are)."""
    return kubectl(
        "exec", GATEWAY, "-c", "gateway", "--", "python", "-c", f"import sys; sys.path.insert(0, 'src'); {code}"
    )


def issue_keys() -> list[str]:
    code = (
        "import json, psycopg, admin, migrate\n"
        "keys = []\n"
        "with psycopg.connect(migrate.database_url(), autocommit=True, connect_timeout=10) as conn:\n"
        f"    for i in range(1, {TEAMS + 1}):\n"
        "        name = f'eks-load-{i:02d}'\n"
        "        if not conn.execute('select 1 from teams where name = %s', (name,)).fetchone():\n"
        "            admin.add_team(conn, name, ['stub'])\n"
        "        key = admin.issue_key(conn, name)\n"
        # the per-key limits (60 requests a minute by default) out of the way, as step 8 and compare_load.py did:
        # this measures the cluster, not the gateway's rate limiter
        "        conn.execute('update api_keys set requests_per_minute = 1000000, tokens_per_minute = 1000000000 "
        "where prefix = %s', (key[:10],))\n"
        "        keys.append(key)\n"
        "print(json.dumps(keys))"
    )
    keys = json.loads(in_gateway(f"exec({code!r})").splitlines()[-1])
    if len(keys) != TEAMS or not all(k.startswith("gw_") for k in keys):
        sys.exit("issuing the test keys failed")
    return keys


def revoke_keys(keys: list[str]) -> None:
    prefixes = [k[:10] for k in keys]
    code = (
        "import psycopg, admin, migrate\n"
        "with psycopg.connect(migrate.database_url(), autocommit=True, connect_timeout=10) as conn:\n"
        f"    for p in {prefixes!r}:\n"
        "        admin.revoke_key(conn, p)\n"
        "print('revoked', len(" + repr(prefixes) + "))"
    )
    print(in_gateway(f"exec({code!r})"))


def wait_for_two_pods(limit_s: int = 600) -> None:
    started = time.monotonic()
    while time.monotonic() - started < limit_s:
        hpa = json.loads(kubectl("get", "hpa", "ai-gateway-gateway", "-o", "json"))
        if hpa["status"].get("currentReplicas") == 2 and hpa["status"].get("desiredReplicas") == 2:
            return
        time.sleep(10)
    sys.exit("the autoscaler didn't settle at 2 pods")


class Timeline(threading.Thread):
    """Every 5 s: the autoscaler's current and desired pods and CPU, the gateway's ready pods, the Ready nodes."""

    def __init__(self):
        super().__init__(daemon=True)
        self.rows: list[dict] = []
        self.stop_flag = threading.Event()
        self.t0 = time.monotonic()

    def sample(self) -> dict:
        hpa = json.loads(kubectl("get", "hpa", "ai-gateway-gateway", "-o", "json", check=False) or "{}")
        status = hpa.get("status", {})
        cpu = next((m["resource"]["current"].get("averageUtilization") for m in status.get("currentMetrics") or []
                    if m.get("type") == "Resource"), None)  # fmt: skip
        ready = json.loads(kubectl("get", GATEWAY, "-o", "json", check=False) or "{}").get("status", {})
        nodes = json.loads(kubectl("get", "nodes", "-o", "json", check=False) or '{"items": []}')["items"]
        ready_nodes = sum(any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])
                          for n in nodes)  # fmt: skip
        return {"t": round(time.monotonic() - self.t0), "pods": status.get("currentReplicas"),
                "desired": status.get("desiredReplicas"), "cpu_pct": cpu, "ready": ready.get("readyReplicas"),
                "nodes": ready_nodes}  # fmt: skip

    def run(self):
        while not self.stop_flag.is_set():
            self.rows.append(self.sample())
            self.stop_flag.wait(5)

    def stop(self) -> list[dict]:
        self.stop_flag.set()
        self.join(timeout=30)
        return self.rows


def job_manifest(name: str, env: dict[str, str]) -> dict:
    args = ["run", "--summary-trend-stats", "avg,med,p(95),p(99),max", "/scripts/run.js"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name},
        "spec": {"backoffLimit": 0, "template": {"spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "k6", "image": K6_IMAGE, "args": args,
                "env": [{"name": "BASE_URL", "value": "http://ai-gateway-gateway:8000"},
                        {"name": "GATEWAY_KEYS", "valueFrom": {"secretKeyRef": {"name": "eks-load-keys",
                                                                                "key": "GATEWAY_KEYS"}}}],
                "resources": {"requests": {"cpu": "500m", "memory": "256Mi"},
                              "limits": {"cpu": "1", "memory": "512Mi"}},
                "volumeMounts": [{"name": "scripts", "mountPath": "/scripts"}],
            }],
            "volumes": [{"name": "scripts", "configMap": {"name": "eks-load-scripts"}}],
        }}},
    }  # fmt: skip


def read_summary(logs: str) -> dict:
    line = next((x for x in logs.splitlines() if x.startswith("K6_SUMMARY ")), None)
    if line is None:
        sys.exit("k6 printed no summary:\n" + logs[-2000:])
    m = json.loads(line[len("K6_SUMMARY ") :])["metrics"]
    duration = m["http_req_duration"]["values"]
    statuses = re.findall(r"status (\d{3}):", logs)
    return {
        "requests": int(m["http_reqs"]["values"]["count"]),
        "failed_pct": round(100 * m["http_req_failed"]["values"]["rate"], 2),
        "failed": int(round(m["http_req_failed"]["values"]["rate"] * m["http_reqs"]["values"]["count"])),
        "not_started": int(m.get("dropped_iterations", {}).get("values", {}).get("count", 0)),
        "p50_ms": round(duration["med"], 1), "p95_ms": round(duration["p(95)"], 1),
        "p99_ms": round(duration["p(99)"], 1), "max_ms": round(duration["max"], 1),
        "error_statuses": {s: statuses.count(s) for s in sorted(set(statuses))},
        "transport_errors": len(re.findall(r"request failed|EOF|connection reset|i/o timeout", logs)),
    }  # fmt: skip


def measure(test: str) -> dict:
    ensure_context()
    spec = TESTS[test]
    keys = issue_keys()
    name = f"k6-{test}"
    try:
        kubectl("apply", "-f", "-", input=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "eks-load-keys"},
            "type": "Opaque", "stringData": {"GATEWAY_KEYS": ",".join(keys)}}))  # fmt: skip
        kubectl("apply", "-f", "-", input=json.dumps({
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "eks-load-scripts"},
            "data": {"chat.js": (AI_GATEWAY / "load" / "chat.js").read_text(encoding="utf-8"),
                     "run.js": WRAPPER}}))  # fmt: skip
        print("waiting for the autoscaler to settle at 2 pods")
        wait_for_two_pods()
        kubectl("delete", "job", name, "--ignore-not-found", "--wait=true")
        timeline = Timeline()
        timeline.start()
        started = dt.datetime.now(dt.UTC)
        kubectl("apply", "-f", "-", input=json.dumps(job_manifest(name, spec["env"])))
        restarts = []
        for at in spec["restarts_at"]:
            time.sleep(max(0, at - (time.monotonic() - timeline.t0)))
            t = time.monotonic()
            kubectl("rollout", "restart", GATEWAY)
            kubectl("rollout", "status", GATEWAY, "--timeout=5m")
            restarts.append({"at_s": at, "took_s": round(time.monotonic() - t, 1)})
            print(f"rolling update at {at} s took {restarts[-1]['took_s']} s")
        # k6 exits non-zero when a threshold fails, and the Job is then Failed, not Complete: wait for either
        while not json.loads(kubectl("get", f"job/{name}", "-o", "json"))["status"].get("conditions"):
            time.sleep(10)
        rows = timeline.stop()
        logs = kubectl("logs", f"job/{name}", "--tail=-1")
        events = kubectl("get", "events", "--field-selector", "involvedObject.kind=HorizontalPodAutoscaler",
                         "-o", "json")  # fmt: skip
    finally:
        revoke_keys(keys)
        kubectl("delete", "secret", "eks-load-keys", "configmap", "eks-load-scripts", "--ignore-not-found")
    scale_events = [{"time": e.get("lastTimestamp"), "message": e.get("message")}
                    for e in json.loads(events)["items"]
                    if (e.get("lastTimestamp") or "") >= started.isoformat(timespec="seconds")[:19]]  # fmt: skip
    return {"test": test, "date": started.isoformat(timespec="seconds"), "k6_env": spec["env"],
            "summary": read_summary(logs), "restarts": restarts, "scale_events": scale_events,
            "timeline": rows}  # fmt: skip


def report(result: dict) -> str:
    s = result["summary"]
    lines = [
        f"# EKS: {result['test']}",
        "",
        f"Generated by `eks/load.py {result['test']}` on {result['date'][:16]} UTC: k6 as a Job in the cluster, "
        f"load/chat.js with {result['k6_env']}, against the `stub` route from {TEAMS} teams. See the README, step 11.",
        "",
        "| Requests | Failed | Not started | p50 ms | p95 ms | p99 ms | max ms | Non-200 answers | Transport errors |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {s['requests']} | {s['failed']} ({s['failed_pct']}%) | {s['not_started']} | {s['p50_ms']} | {s['p95_ms']} "
        f"| {s['p99_ms']} | {s['max_ms']} | {s['error_statuses'] or 'none'} | {s['transport_errors']} |",
        "",
    ]
    if result["restarts"]:
        updates = "; ".join(f"at {r['at_s']} s, took {r['took_s']} s" for r in result["restarts"])
        lines += [f"Rolling updates: {updates}", ""]
    lines += ["Every 15 s (pods: the autoscaler's current/desired; CPU: the pods' average, % of their request):", "",
              "| t (s) | Pods | Desired | CPU % | Ready | Nodes |", "|---|---|---|---|---|---|"]  # fmt: skip
    lines += [f"| {r['t']} | {r['pods']} | {r['desired']} | {r['cpu_pct']} | {r['ready']} | {r['nodes']} |"
              for r in result["timeline"] if r["t"] % 15 < 5]  # fmt: skip
    if result["scale_events"]:
        lines += ["", "Autoscaler events:", ""] + [f"- {e['time']}: {e['message']}" for e in result["scale_events"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("test", choices=TESTS)
    os.chdir(AI_GATEWAY)
    os.environ["AWS_PROFILE"] = "agent-toolkit"
    result = measure(parser.parse_args().test)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{result['test']}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    (OUT / f"{result['test']}.md").write_text(report(result), encoding="utf-8", newline="\n")
    print(report(result))


if __name__ == "__main__":
    main()
