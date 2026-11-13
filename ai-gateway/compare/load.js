// The load comparison (step 10 stage 3): a fixed rate of chat requests at one target, run by src/compare_load.py for
// each target (the gateway, LiteLLM, and the fake upstream directly as the floor) and each rate.
//
//     $env:COMPARE_KEY = "<the target's key>"      # never on the command line
//     k6 run -e BASE_URL=http://127.0.0.1:8101 -e MODEL=chat -e RATE=50 -e HOLD=40s compare/load.js
//
// An arrival-rate scenario: requests start at RATE per second whatever the latency, so a slower target ends up with
// more requests in flight, as real traffic would. Connections are reused (one gateway each, no load balancer), so
// what's measured is the gateways, not TCP setup. http_req_waiting is the time to the first byte of the response:
// for a stream, roughly when the first words arrive.
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL;
const KEY = __ENV.COMPARE_KEY || "none";
const RATE = Number(__ENV.RATE || 20);
const STREAM = __ENV.STREAM === "1";

export const options = {
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
  scenarios: {
    chat: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.HOLD || "40s",
      preAllocatedVUs: Math.min(50, RATE * 2),
      maxVUs: 600,
    },
  },
};

const BODY = JSON.stringify({
  model: __ENV.MODEL || "chat",
  max_tokens: 50,
  stream: STREAM,
  ...(STREAM ? { stream_options: { include_usage: true } } : {}),
  messages: [{ role: "user", content: "load test: hello" }],
});

export function setup() {
  if (!BASE) throw new Error("set BASE_URL");
}

export default function () {
  const res = http.post(`${BASE}/v1/chat/completions`, BODY, {
    headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
    timeout: "30s",
  });
  check(res, { "status 200": (r) => r.status === 200 });
  if (res.status !== 0 && res.status !== 200) {
    console.warn(`status ${res.status}: ${String(res.body).slice(0, 120)}`);
  }
}
