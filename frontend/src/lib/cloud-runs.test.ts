import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import {
  artifactResponse,
  expireStaleRuns,
  getRun,
  listRuns,
  runnerCallback,
  safeArtifactPath,
  startRun,
} from "./cloud-runs.ts";
import { listTasks } from "./cloud-store.ts";
import type { CloudContext, RunnerPayload } from "./cloud-types";
import type { RunDetail } from "./types";

async function fixture() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(
    readFileSync(
      new URL("../../migrations/0001_shared_evals.sql", import.meta.url),
      "utf8",
    ),
  );
  const prepare = (query: string) => {
    let values: any[] = [];
    const statement = {
      bind(...args: any[]) {
        values = args;
        return statement;
      },
      async first() {
        return sql.prepare(query).get(...values) ?? null;
      },
      async all() {
        return { results: sql.prepare(query).all(...values) };
      },
      async run() {
        const result = sql.prepare(query).run(...values);
        return { success: true, meta: { changes: Number(result.changes) } };
      },
    };
    return statement;
  };
  const objects = new Map<
    string,
    { bytes: Uint8Array; customMetadata?: Record<string, string> }
  >();
  const bucket = {
    async put(key: string, value: string | Uint8Array, options: any = {}) {
      if (options.onlyIf && objects.has(key)) return null;
      const object = {
        bytes:
          typeof value === "string" ? new TextEncoder().encode(value) : value,
        customMetadata: options.customMetadata,
      };
      objects.set(key, object);
      return object;
    },
    async head(key: string) {
      return objects.get(key) || null;
    },
    async get(key: string) {
      const value = objects.get(key);
      return value
        ? {
            ...value,
            body: new Response(new Uint8Array(value.bytes).buffer).body,
            async json() {
              return JSON.parse(new TextDecoder().decode(value.bytes));
            },
          }
        : null;
    },
  };
  const ctx = {
    user: "colleague@example.com",
    env: {
      DB: {
        prepare,
        async batch(statements: any[]) {
          const results = [];
          for (const statement of statements)
            results.push(await statement.run());
          return results;
        },
      },
      ARTIFACTS: bucket,
      RUNNERS: {},
      APP_ORIGIN: "https://evals.example",
      ANTHROPIC_API_KEY: "test-anthropic-secret",
      E2B_API_KEY: "test-e2b-secret",
      EXPO_TOKEN: "test-expo-secret",
      TYPESAFE_API_KEY: "test-judge-secret",
    },
  } as unknown as CloudContext;
  await listTasks(ctx);
  const committed = () =>
    sql
      .prepare(
        "UPDATE tasks SET base_commit=?,committed_revision=revision WHERE id=?",
      )
      .run("a".repeat(40), "coffee-timer");
  let token = "",
    payload: RunnerPayload | null = null;
  const launch = async (
    _env: unknown,
    value: RunnerPayload,
    capability: string,
  ) => {
    payload = value;
    token = capability;
  };
  const start = () =>
    startRun(
      ctx,
      {
        taskId: "coffee-timer",
        revision: 1,
        agent: "claude-code",
        model: "anthropic/claude-opus-5-5",
      },
      launch,
    );
  const callback = (id: string, value: unknown, capability = token) =>
    runnerCallback(
      new Request(`https://evals.example/api/runner-callback/${id}`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${capability}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(value),
      }),
      ctx.env,
      id,
    );
  const upload = (id: string, path: string, body: string) =>
    runnerCallback(
      new Request(
        `https://evals.example/api/runner-callback/${id}?path=${encodeURIComponent(path)}`,
        { method: "PUT", headers: { authorization: `Bearer ${token}` }, body },
      ),
      ctx.env,
      id,
    );
  return {
    ctx,
    sql,
    bucket,
    objects,
    committed,
    start,
    launch,
    callback,
    upload,
    get token() {
      return token;
    },
    get payload() {
      return payload;
    },
  };
}

function detail(id: string): RunDetail {
  return {
    id,
    task: "coffee-timer",
    agent: "claude-code",
    model: "anthropic/claude-opus-5-5",
    startedAt: new Date().toISOString(),
    status: "passed",
    score: 1,
    costUsd: 0.2,
    durationSec: 15,
    criteria: [],
    steps: [],
    log: "test-expo-secret",
    artifacts: [],
    configFiles: [],
    error: null,
  };
}

test("runs require committed revision, preserve immutable files, and allow only one active job", async () => {
  const f = await fixture();
  await assert.rejects(f.start(), /Commit the current draft/);
  f.committed();
  const { id } = await f.start();
  assert.equal(f.payload!.commitSha, "a".repeat(40));
  const snapshot = f.sql
    .prepare("SELECT files_json,callback_hash FROM runs WHERE id=?")
    .get(id)!;
  assert.notEqual(snapshot.callback_hash, f.token);
  f.sql.prepare("UPDATE tasks SET files_json='[]',revision=revision+1").run();
  assert.notEqual(snapshot.files_json, "[]");
  assert.equal((await getRun(f.ctx, id)).configFiles.length, 6);
  f.sql.prepare("UPDATE tasks SET revision=1,committed_revision=1").run();
  await assert.rejects(f.start(), /Another eval/);
  f.sql.close();
});

test("unknown capability and changed terminal callbacks cannot modify completed runs", async () => {
  const f = await fixture();
  f.committed();
  const { id } = await f.start();
  assert.equal(
    (await f.callback(id, { status: "running" }, "wrong-token")).status,
    401,
  );
  assert.equal((await f.callback(id, { status: "running" })).status, 200);
  const response = await f.callback(id, {
    status: "completed",
    detail: detail(id),
  });
  assert.equal(response.headers.get("x-run-complete"), "1");
  assert.equal((await getRun(f.ctx, id)).status, "passed");
  assert.equal((await getRun(f.ctx, id)).log, "[redacted]");
  await f.callback(id, { status: "failed", error: "late duplicate" });
  await f.callback(id, { status: "running" });
  assert.equal((await getRun(f.ctx, id)).status, "passed");
  assert.equal((await listRuns(f.ctx))[0].score, 1);
  const compact = String(
    f.sql.prepare("SELECT detail_json FROM runs WHERE id=?").get(id)!
      .detail_json,
  );
  assert.ok(compact.length < 200);
  f.sql.close();
});

test("artifact uploads reject traversal, redact credentials, and stay immutable across retries", async () => {
  const f = await fixture();
  f.committed();
  const { id } = await f.start();
  for (const path of [
    "../.env",
    "tasks/.env",
    "/absolute",
    "nested/credentials.json",
    "a\\b",
    "a//b",
  ]) {
    assert.equal(safeArtifactPath(path), false);
    assert.equal((await f.upload(id, path, "bad")).status, 400);
  }
  assert.equal(
    (
      await f.upload(
        id,
        "jobs/trial/result.json",
        JSON.stringify({ value: "test-expo-secret" }),
      )
    ).status,
    200,
  );
  assert.equal(
    (
      await f.upload(
        id,
        "jobs/trial/result.json",
        JSON.stringify({ value: "test-expo-secret" }),
      )
    ).status,
    200,
  );
  assert.equal(
    (await f.upload(id, "jobs/trial/result.json", "changed")).status,
    409,
  );
  const response = await artifactResponse(f.ctx, id, "jobs/trial/result.json");
  assert.match(response.headers.get("content-disposition")!, /^attachment/);
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.deepEqual(JSON.parse(await response.text()), { value: "[redacted]" });
  f.sql.close();
});

test("uncertain startup keeps the active slot until timeout watchdog", async () => {
  const f = await fixture();
  f.committed();
  const { id } = await startRun(
    f.ctx,
    {
      taskId: "coffee-timer",
      revision: 1,
      agent: "claude-code",
      model: "model",
    },
    async () => {
      throw new Error("connection lost after dispatch test-expo-secret");
    },
  );
  let run = await getRun(f.ctx, id);
  assert.equal(run.status, "running");
  assert.match(run.error!, /unconfirmed/);
  assert.ok(!run.error!.includes("test-expo-secret"));
  await assert.rejects(f.start(), /Another eval/);
  f.sql
    .prepare("UPDATE runs SET created_at=? WHERE id=?")
    .run(new Date(Date.now() - 81 * 60 * 1000).toISOString(), id);
  await expireStaleRuns(f.ctx.env);
  run = await getRun(f.ctx, id);
  assert.equal(run.status, "interrupted");
  await f.start();
  f.sql.close();
});

test("R2 failures keep the run active and callback retry finishes persistence", async () => {
  const f = await fixture();
  f.committed();
  const { id } = await f.start();
  const put = f.bucket.put;
  f.bucket.put = async () => {
    throw new Error("R2 temporarily unavailable");
  };
  assert.equal(
    (await f.callback(id, { status: "completed", detail: detail(id) })).status,
    500,
  );
  assert.equal((await getRun(f.ctx, id)).status, "running");
  f.bucket.put = put;
  assert.equal(
    (await f.callback(id, { status: "completed", detail: detail(id) })).status,
    200,
  );
  assert.equal((await getRun(f.ctx, id)).status, "passed");
  f.sql.close();
});

test("expired callback cannot reopen or upload to an old run", async () => {
  const f = await fixture();
  f.committed();
  const { id } = await f.start();
  f.sql
    .prepare("UPDATE runs SET created_at=? WHERE id=?")
    .run(new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(), id);
  assert.equal((await f.callback(id, { status: "running" })).status, 401);
  assert.equal((await f.upload(id, "source.ts", "text")).status, 401);
  f.sql.close();
});
