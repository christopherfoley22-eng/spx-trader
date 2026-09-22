"use strict";

// Offline browser-script timing test. The HTTP/controller path is covered by
// executor_local_service_test.py; this exercises the actual inline UI script.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {randomUUID} = require("node:crypto");

const html = fs.readFileSync(require("node:path").join(__dirname, "executor_local_ui.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function status(state, direction = "CALL") {
  const active = state === "OPEN";
  return {
    state: active ? "OPEN" : "READY", lifecycle: active ? "OPEN" : "FLAT",
    ready: !active, reason: active ? "A simulated lifecycle is active" : null,
    trade_count: active ? 1 : 0, trade_limit: 2,
    position: active ? {
      direction, quantity: 11, strike: 5000, simulated_entry_ask: 10,
      entry_spx: 5000, high_water_spx: "5000", maximum_favorable_points: "0",
      strategy: "INITIAL_STOP", fixed_profit_floor_points: null, exit_reason: null,
    } : null,
    activity: [], development_status: null, fixture: "immediate_winner",
    fixtures: ["immediate_winner"], replay_index: active ? 0 : null,
    replay_total: active ? 3 : null,
  };
}

function flatAfterTrade() {
  return {
    ...status("READY"), state: "CONFIRMED FLAT", trade_count: 1,
    ready: true, replay_index: null, replay_total: null,
  };
}

function node() {
  return {
    disabled: false, hidden: false, textContent: "", value: "", children: [],
    replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); },
  };
}

async function settle() {
  for (let index = 0; index < 8; index++) {
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function scenario(direction, result = "flat") {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  };
  let server = status("READY", direction);
  let releaseIntent;
  const intentGate = new Promise(resolve => { releaseIntent = resolve; });
  const requests = [];
  const clone = value => JSON.parse(JSON.stringify(value));
  const fetch = async (path, options = {}) => {
    requests.push({path, serverState: server.lifecycle});
    if (path === "/api/status") {
      return {ok: true, json: async () => clone(server)};
    }
    if (path === "/api/execute") {
      assert.equal(JSON.parse(options.body).direction, direction);
      await intentGate;
      if (result === "intent-error") {
        return {ok: false, json: async () => ({error: "Intent blocked", retryable: true})};
      }
      server = (result === "run-error" || result === "run-open")
        ? status("OPEN", direction) : flatAfterTrade();
      return {ok: true, json: async () => ({result: "ACCEPTED"})};
    }
    if (path === "/api/demo/run") {
      assert.equal(server.lifecycle, "OPEN", "Run requires the accepted intent");
      if (result === "run-error") {
        return {ok: false, json: async () => ({error: "Replay conflict", retryable: true})};
      }
      if (result !== "run-open") server = flatAfterTrade();
      return {ok: true, json: async () => ({})};
    }
    throw Error("Unexpected request: " + path);
  };
  const document = {
    querySelector: () => ({content: "synthetic-csrf"}),
    getElementById: get,
    createElement: () => node(),
  };
  vm.runInNewContext(script, {document, fetch, setInterval: () => 0,
                              crypto: {randomUUID}, console});
  await settle();
  assert.equal(get("state").textContent, "READY");
  const clicked = get(direction === "CALL" ? "calls" : "puts").onclick();
  assert.equal(get("run").hidden, true);
  assert.equal(get("queue-run").hidden, false);
  get("queue-run").onclick();
  get("queue-run").onclick(); // repeated tap still queues one Run
  assert.match(get("progress").textContent, /queued/i);
  await settle();
  assert.equal(requests.filter(item => item.path === "/api/demo/run").length, 0);
  releaseIntent();
  await clicked;
  await settle();
  const runRequests = requests.filter(item => item.path === "/api/demo/run");
  assert.equal(runRequests.length,
               result === "run-error" || result === "run-open" ? 1 : 0);
  if (result === "flat") {
    assert.equal(requests.filter(item => item.path === "/api/execute").length, 1);
    assert.equal(get("state").textContent, "CONFIRMED FLAT");
    assert.equal(get("lifecycle").textContent, "Lifecycle: FLAT");
    assert.equal(get("count").textContent, "1/2 trades");
  } else if (result === "run-open") {
    assert.equal(get("state").textContent, "OPEN",
                 "HTTP success must not imply confirmed flat");
    assert.equal(get("lifecycle").textContent, "Lifecycle: OPEN");
  } else {
    assert.match(get("error").textContent, /retry/i);
    assert.equal(get("state").textContent,
                 result === "intent-error" ? "READY" : "OPEN");
  }
}

(async () => {
  await scenario("CALL");
  await scenario("PUT");
  await scenario("CALL", "run-error");
  await scenario("CALL", "run-open");
  await scenario("PUT", "intent-error");
  process.stdout.write("OFFLINE UI QUEUED RUN TIMING PASS\n");
})().catch(error => { console.error(error); process.exitCode = 1; });
