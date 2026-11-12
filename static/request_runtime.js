/* Request coordination shared by the page and offline Node tests. */
(function (root) {
  function create(fetcher, timeoutMs = 30000) {
    const pending = new Map();
    function run(key, work) {
      if (pending.has(key)) return pending.get(key);
      const task = Promise.resolve().then(work).finally(() => pending.delete(key));
      pending.set(key, task);
      return task;
    }
    function get(path) {
      return run("GET " + path, async () => {
        const controller = new AbortController();
        let timer;
        const timeout = new Promise((_, reject) => {
          timer = setTimeout(() => {
            controller.abort();
            reject(new Error("读取超时，请稍后刷新"));
          }, timeoutMs);
        });
        try {
          return await Promise.race([timeout, (async () => {
            const response = await fetcher(path, { signal: controller.signal });
            const body = await response.json();
            if (!response.ok || !body.ok) {
              throw new Error((body.error && body.error.message) || "请求失败");
            }
            return body;
          })()]);
        } finally { clearTimeout(timer); }
      }).then((body) => JSON.parse(JSON.stringify(body)));
    }
    return { get, run };
  }
  const api = { create };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RequestRuntime = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
