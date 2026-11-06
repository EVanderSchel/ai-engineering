// A k6 load test of the gateway (step 8 stage 5): chat requests to the `stub` route, which answers offline and free, so
// this measures the gateway (auth, admission, the ledger), not a provider.
//
//     $env:GATEWAY_KEY = "<a key for a team allowed `stub`>"     # never on the command line
//     $env:GATEWAY_KEYS = "<key1>,<key2>,..."                     # or many teams' keys, used in turn
//     k6 run load/chat.js                                          # the default profile below
//     k6 run -e RATE=40 -e HOLD=1m load/chat.js                    # a fixed rate, for calibration
//
// The rate is in requests per second, whatever the latency (an arrival-rate scenario): a slower gateway gets more
// requests in flight rather than fewer requests, as real traffic would. Every request opens a new connection
// (noConnectionReuse), because a Service balances connections, not requests (stage 2): one kept-alive connection
// would put the whole test on one pod.
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8088";
// One key, or several separated by commas (GATEWAY_KEYS), used in turn: one team at a high rate updates the same rate
// bucket and budget rows on every request, which PostgreSQL serializes (stage 5); many teams don't.
const KEYS = (__ENV.GATEWAY_KEYS || __ENV.GATEWAY_KEY || "").split(",").filter((k) => k);
const RATE = __ENV.RATE ? Number(__ENV.RATE) : null;

const stages = RATE
  ? [{ target: RATE, duration: "10s" }, { target: RATE, duration: __ENV.HOLD || "1m" }]
  : [
      { target: 10, duration: "30s" }, // light: the HPA's floor of 2 pods is plenty
      { target: Number(__ENV.PEAK || 120), duration: "1m" }, // ramp up
      { target: Number(__ENV.PEAK || 120), duration: __ENV.HOLD || "3m" }, // hold: the HPA adds pods
      { target: 10, duration: "30s" }, // back down: the HPA removes them after its stabilization window
      { target: 10, duration: __ENV.TAIL || "3m" },
    ];

export const options = {
  noConnectionReuse: true,
  scenarios: {
    chat: {
      executor: "ramping-arrival-rate",
      startRate: 5,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 300,
      stages,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"], // fewer than 1% failed
    http_req_duration: ["p(95)<1000"], // 95% answered within a second
  },
};

const BODY = JSON.stringify({
  model: "stub",
  max_tokens: 16,
  messages: [{ role: "user", content: "load test: hello" }],
});

export function setup() {
  if (!KEYS.length) throw new Error("set GATEWAY_KEY (or GATEWAY_KEYS) in the environment");
}

let calls = 0;

export default function () {
  const key = KEYS[(__VU + calls++) % KEYS.length];
  const res = http.post(`${BASE}/v1/chat/completions`, BODY, {
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    timeout: "10s",
  });
  check(res, { "status 200": (r) => r.status === 200 });
  // Transport failures (EOF, reset, timeout) k6 logs by itself; an answer that isn't 200 is logged here, so a run's
  // failures can be told apart (stage 6), not only counted.
  if (res.status !== 0 && res.status !== 200) {
    console.warn(`status ${res.status}: ${String(res.body).slice(0, 120)}`);
  }
}
