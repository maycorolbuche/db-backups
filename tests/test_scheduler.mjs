import assert from "node:assert/strict";
import { test } from "node:test";

import scheduler from "../cloudflare/scheduler.mjs";

const controller = { scheduledTime: Date.UTC(2026, 10, 16, 14, 3) };
const env = {
  GITHUB_REPOSITORY: "maycorolbuche/db-backups",
  GITHUB_REF: "main",
  GITHUB_DISPATCH_TOKEN: " test-token ",
};

test("dispara a rotina automática sem selecionar um banco manualmente", async (t) => {
  const handler = t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  await scheduler.scheduled(controller, env);
  const [[url, request]] = handler.mock.calls.map((call) => call.arguments);
  assert.equal(url, "https://api.github.com/repos/maycorolbuche/db-backups/actions/workflows/backups.yml/dispatches");
  assert.equal(request.method, "POST");
  assert.equal(request.headers.Authorization, "Bearer test-token");
  assert.deepEqual(JSON.parse(request.body), { ref: "main" });
});

test("registra falha sem expor o token quando o GitHub rejeita o disparo", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("test-token", { status: 403 }));
  await assert.rejects(scheduler.scheduled(controller, env), {
    message: "GitHub recusou o disparo de backups (HTTP 403).",
  });
});

test("não faz uma chamada sem o secret configurado", async (t) => {
  const handler = t.mock.method(globalThis, "fetch");
  await assert.rejects(scheduler.scheduled(controller, { ...env, GITHUB_DISPATCH_TOKEN: " " }), /secret/);
  assert.equal(handler.mock.callCount(), 0);
});
