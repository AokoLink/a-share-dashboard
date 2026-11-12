const { test } = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("../../static/request_runtime.js");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const response = (data) => ({ ok: true, json: async () => ({ ok: true, data }) });

test("duplicate GET shares one fetch and returns isolated data", async () => {
  const gate = deferred(); let calls = 0;
  const api = create(() => { calls++; return gate.promise; });
  const first = api.get("/saved"), second = api.get("/saved");
  gate.resolve(response({ rows: [] }));
  const [a, b] = await Promise.all([first, second]);
  assert.equal(calls, 1);
  a.data.rows.push("local");
  assert.deepEqual(b.data.rows, []);
  await api.get("/saved");
  assert.equal(calls, 2); // Only in-flight dedupe; no stale page cache.
});

test("distinct URLs load independently", async () => {
  let calls = 0;
  const api = create(async () => { calls++; return response({}); });
  await Promise.all([api.get("/page?cursor=1"), api.get("/page?cursor=2")]);
  assert.equal(calls, 2);
});

test("failures release the request for retry", async () => {
  let calls = 0;
  const api = create(async () => {
    if (++calls === 1) throw new Error("offline");
    return response({});
  });
  await assert.rejects(api.get("/saved"), /offline/);
  await api.get("/saved");
  assert.equal(calls, 2);
});

test("HTTP failures with success-shaped body still fail", async () => {
  const api = create(async () => ({ ...response({}), ok: false }));
  await assert.rejects(api.get("/saved"), /请求失败/);
});

test("timeout aborts a stuck fetch and permits retry", async () => {
  let signal, calls = 0;
  const api = create((_, options) => {
    signal = options.signal;
    return ++calls === 1 ? new Promise(() => {}) : Promise.resolve(response({}));
  }, 15);
  await assert.rejects(api.get("/saved"), /超时/);
  assert.equal(signal.aborted, true);
  await api.get("/saved");
  assert.equal(calls, 2);
});

test("timeout covers a stuck response body", async () => {
  const api = create(async () => ({ ok: true, json: () => new Promise(() => {}) }), 15);
  await assert.rejects(api.get("/saved"), /超时/);
});

test("refresh coordination prevents duplicate work and resets after failure", async () => {
  const api = create(() => {}); const gate = deferred(); let calls = 0;
  const work = () => { calls++; return gate.promise; };
  const a = api.run("refresh", work), b = api.run("refresh", work);
  assert.equal(a, b);
  gate.reject(new Error("failed"));
  await assert.rejects(a, /failed/);
  await api.run("refresh", () => { calls++; });
  assert.equal(calls, 2);
});
