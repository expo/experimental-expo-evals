import type { CloudContext, CloudEnv, RunnerPayload } from "./cloud-types";
import type { Agent, RunDetail, RunSummary } from "./types";
import { getTask } from "./cloud-store.ts";
import { sandboxProvider } from "./task-provider.ts";

type RunRow = {
  id: string;
  task_id: string;
  commit_sha: string;
  files_json: string;
  agent: string;
  model: string;
  status: string;
  created_at: string;
  updated_at: string;
  callback_hash: string | null;
  detail_json: string | null;
  error: string | null;
};
type Launcher = (
  env: CloudEnv,
  payload: RunnerPayload,
  token: string,
) => Promise<unknown>;
const active = (status: string) => status === "queued" || status === "running";
const validId = (id: string) =>
  typeof id === "string" && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}$/.test(id);
const finite = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const images: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  avif: "image/avif",
};
const byteLength = (value: string) =>
  new TextEncoder().encode(value).byteLength;
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "Unexpected runner error.";
const parse = (value: string | null): Record<string, any> => {
  try {
    return JSON.parse(value || "{}");
  } catch {
    return {};
  }
};
const hash = async (value: string | Uint8Array) =>
  [
    ...new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        typeof value === "string"
          ? new TextEncoder().encode(value)
          : (value as BufferSource),
      ),
    ),
  ]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
const redact = (env: CloudEnv, text: string) =>
  [
    env.GITHUB_TOKEN,
    env.GITHUB_APP_PRIVATE_KEY,
    env.DASHBOARD_PASSWORD,
    env.SESSION_SECRET,
    env.ANTHROPIC_API_KEY,
    env.OPENAI_API_KEY,
    env.E2B_API_KEY,
    env.EXPO_TOKEN,
    env.TYPESAFE_API_KEY,
  ]
    .filter((value): value is string => !!value)
    .sort((a, b) => b.length - a.length)
    .reduce((result, secret) => result.split(secret).join("[redacted]"), text);
const scrub = <T>(env: CloudEnv, value: T): T =>
  JSON.parse(
    JSON.stringify(value, (_key, item) =>
      typeof item === "string" ? redact(env, item) : item,
    ),
  );
const headers = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};
const reply = (status: number, body: unknown, completed = false) =>
  Response.json(body, {
    status,
    headers: { ...headers, ...(completed ? { "x-run-complete": "1" } : {}) },
  });

export function safeArtifactPath(path: string) {
  return (
    typeof path === "string" &&
    path.length > 0 &&
    path.length <= 1000 &&
    !/[\\\x00-\x1f]/.test(path) &&
    path
      .split("/")
      .every(
        (part) =>
          part &&
          !part.startsWith(".") &&
          !["node_modules", "__pycache__"].includes(part) &&
          !/(?:^|[-_])(credentials|secrets?)(?:[._-]|$)/i.test(part),
      )
  );
}

async function row(env: CloudEnv, id: string) {
  if (!validId(id)) throw new Error("Invalid run ID.");
  return env.DB.prepare("SELECT * FROM runs WHERE id=?")
    .bind(id)
    .first<RunRow>();
}

function summary(run: RunRow): RunSummary & { commitSha: string } {
  const detail = parse(run.detail_json);
  return {
    id: run.id,
    task: run.task_id,
    agent: run.agent,
    model: run.model,
    startedAt: run.created_at,
    status: run.status as RunSummary["status"],
    score: finite(detail.score),
    durationSec: finite(detail.durationSec),
    costUsd: finite(detail.costUsd),
    commitSha: run.commit_sha,
  };
}

function fallback(run: RunRow): RunDetail {
  return {
    ...summary(run),
    criteria: [],
    steps: [],
    artifacts: [],
    log: "",
    error: run.error,
    configFiles: parseFiles(run.files_json),
  };
}

function parseFiles(value: string) {
  try {
    const result = JSON.parse(value);
    return Array.isArray(result) ? result : [];
  } catch {
    return [];
  }
}

export async function listRuns(ctx: CloudContext) {
  const result = await ctx.env.DB.prepare(
    "SELECT * FROM runs ORDER BY created_at DESC LIMIT 100",
  ).all<RunRow>();
  return result.results.map(summary);
}

export async function getRun(
  ctx: CloudContext,
  id: string,
): Promise<RunDetail> {
  const run = await row(ctx.env, id);
  if (!run) throw new Error("Run not found.");
  const stored = await ctx.env.ARTIFACTS.get(`runs/${id}/detail.json`);
  if (!stored) return scrub(ctx.env, fallback(run));
  const detail = await stored.json<RunDetail>();
  return scrub(ctx.env, {
    ...detail,
    ...summary(run),
    error: run.error || detail.error,
  });
}

export async function startRun(
  ctx: CloudContext,
  input: { taskId: string; revision: number; agent: Agent; model: string },
  launch?: Launcher,
) {
  if (ctx.env.LOCAL_DEV === "true")
    throw new Error(
      "The local web preview cannot start Harbor containers. Run evals from the deployed dashboard, or use the native Harbor CLI locally.",
    );
  if (
    !input ||
    !validId(input.taskId) ||
    !Number.isInteger(input.revision) ||
    !["claude-code", "codex"].includes(input.agent) ||
    typeof input.model !== "string" ||
    !input.model.trim() ||
    input.model.length > 200 ||
    /[\x00-\x1f]/.test(input.model)
  )
    throw new Error("Choose a valid task revision, agent, and model.");
  if (
    !ctx.env.APP_ORIGIN ||
    !ctx.env.EXPO_TOKEN ||
    !ctx.env.TYPESAFE_API_KEY ||
    !(input.agent === "codex"
      ? ctx.env.OPENAI_API_KEY
      : ctx.env.ANTHROPIC_API_KEY)
  )
    throw new Error(
      "Configure the application origin and runner credentials before running an eval.",
    );
  const task = await getTask(ctx, input.taskId);
  const provider = sandboxProvider(task.files);
  if (provider === "e2b" ? !ctx.env.E2B_API_KEY : !ctx.env.EAS_PROJECT_ID)
    throw new Error(
      provider === "e2b"
        ? "Configure E2B_API_KEY before running an E2B eval."
        : "Configure EAS_PROJECT_ID before running an EAS Workflows eval.",
    );
  if (
    task.revision !== input.revision ||
    task.committedRevision !== task.revision ||
    !task.baseCommit ||
    !/^[a-f0-9]{40}$/.test(task.baseCommit)
  )
    throw new Error(
      "Commit the current draft before running it. Runs use an exact saved Git commit.",
    );
  const id = `run-${crypto.randomUUID()}`,
    token = crypto.randomUUID() + crypto.randomUUID(),
    now = new Date().toISOString();
  try {
    await ctx.env.DB.prepare(
      `INSERT INTO runs (id,task_id,commit_sha,files_json,agent,model,status,created_at,updated_at,callback_hash) VALUES (?,?,?,?,?,?,'queued',?,?,?)`,
    )
      .bind(
        id,
        task.id,
        task.baseCommit,
        JSON.stringify(task.files),
        input.agent,
        input.model.trim(),
        now,
        now,
        await hash(token),
      )
      .run();
  } catch (error) {
    if (/unique|constraint/i.test(errorText(error)))
      throw new Error(
        "Another eval is queued or running. Wait for it to finish before starting a new run.",
      );
    throw error;
  }
  try {
    const launcher =
      launch ?? (await import("./runner-container")).launchContainer;
    const result = await launcher(
      ctx.env,
      {
        runId: id,
        taskId: task.id,
        commitSha: task.baseCommit,
        files: task.files,
        agent: input.agent,
        model: input.model.trim(),
      },
      token,
    );
    if (
      result &&
      typeof result === "object" &&
      "status" in result &&
      result.status === "failed"
    ) {
      if (
        !("runId" in result) ||
        result.runId !== id ||
        !("error" in result) ||
        typeof result.error !== "string"
      ) {
        throw new Error("Runner returned an invalid launch result.");
      }
      // A structured failure is only returned before POST /run is dispatched.
      // Revoke its unused capability and allow the next run immediately.
      await ctx.env.DB.prepare(
        "UPDATE runs SET status='error',error=?,callback_hash=NULL,updated_at=? WHERE id=? AND status='queued'",
      )
        .bind(
          redact(ctx.env, `Runner could not start: ${result.error}`),
          new Date().toISOString(),
          id,
        )
        .run();
    }
  } catch (error) {
    // A timeout might happen after /run was accepted. Keep the slot reserved until
    // an authenticated callback or the watchdog resolves that uncertain outcome.
    await ctx.env.DB.prepare(
      "UPDATE runs SET error=?,updated_at=? WHERE id=? AND status='queued'",
    )
      .bind(
        redact(
          ctx.env,
          `Runner launch is unconfirmed: ${errorText(error)} The run remains reserved while waiting for a callback.`,
        ),
        new Date().toISOString(),
        id,
      )
      .run();
  }
  return { id };
}

async function boundedBody(request: Request, limit: number) {
  const declared = Number(request.headers.get("content-length") || "0");
  if (declared > limit) throw new Error("Payload exceeds the size limit.");
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader(),
    chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const item = await reader.read();
    if (item.done) break;
    total += item.value.byteLength;
    if (total > limit) {
      await reader.cancel();
      throw new Error("Payload exceeds the size limit.");
    }
    chunks.push(item.value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.length;
  }
  return body;
}

async function authorized(request: Request, run: RunRow) {
  const authorization = request.headers.get("authorization") || "";
  if (
    !authorization.startsWith("Bearer ") ||
    authorization.length > 256 ||
    !run.callback_hash ||
    Date.now() - Date.parse(run.created_at) > 24 * 60 * 60 * 1000
  )
    return false;
  const actual = await hash(authorization.slice(7)),
    expected = run.callback_hash;
  let difference = actual.length ^ expected.length;
  for (let index = 0; index < actual.length; index++)
    difference |= actual.charCodeAt(index) ^ (expected.charCodeAt(index) || 0);
  return difference === 0;
}

function normalizedDetail(
  run: RunRow,
  value: unknown,
  failed: boolean,
  message?: unknown,
): RunDetail {
  const item = value as RunDetail | null;
  if (!item && !failed)
    throw new Error("Completed runs require a result detail.");
  if (
    item &&
    (!["passed", "failed", "error"].includes(item.status) ||
      !Array.isArray(item.criteria) ||
      !Array.isArray(item.steps) ||
      !Array.isArray(item.artifacts) ||
      !Array.isArray(item.configFiles))
  )
    throw new Error("Invalid run detail.");
  const error =
    typeof message === "string"
      ? message.slice(0, 5000)
      : item?.error || (failed ? "Runner failed. Check the logs." : null);
  return {
    ...fallback(run),
    status: failed || error ? "error" : item!.status,
    score: finite(item?.score),
    durationSec: finite(item?.durationSec),
    costUsd: finite(item?.costUsd),
    log: typeof item?.log === "string" ? item.log.slice(-60_000) : "",
    error,
    criteria: (item?.criteria || []).slice(0, 100).map((criterion) => ({
      name: String(criterion.name || "").slice(0, 200),
      description: String(criterion.description || "").slice(0, 4000),
      score: finite(criterion.score) ?? 0,
      probability: finite(criterion.probability),
    })),
    steps: (item?.steps || []).slice(0, 500).map((step) => ({
      id: String(step.id).slice(0, 100),
      role: String(step.role).slice(0, 30),
      text: String(step.text).slice(0, 40_000),
    })),
    artifacts: (item?.artifacts || [])
      .slice(0, 400)
      .filter((file) => file && safeArtifactPath(file.path))
      .map((file) => ({
        path: file.path,
        name: file.path.split("/").at(-1)!,
        image: !!images[file.path.split(".").at(-1)!.toLowerCase()],
      })),
    configFiles: (item?.configFiles || [])
      .slice(0, 30)
      .filter(
        (file) =>
          file &&
          safeArtifactPath(file.path) &&
          typeof file.content === "string" &&
          byteLength(file.content) <= 1_000_000,
      )
      .map((file) => ({ path: file.path, content: file.content })),
  };
}

export async function runnerCallback(
  request: Request,
  env: CloudEnv,
  id: string,
): Promise<Response> {
  if (!validId(id)) return reply(404, { error: "Unknown run." });
  const run = await row(env, id);
  if (!run || !(await authorized(request, run)))
    return reply(401, { error: "Invalid or expired run capability." });
  if (!["POST", "PUT"].includes(request.method))
    return reply(405, { error: "Method not allowed." });
  try {
    if (request.method === "PUT") {
      const path = new URL(request.url).searchParams.get("path") || "";
      if (!safeArtifactPath(path))
        return reply(400, { error: "Invalid artifact path." });
      let body = await boundedBody(request, 20 * 1024 * 1024);
      const image = images[path.split(".").at(-1)!.toLowerCase()];
      if (!image) {
        let content = new TextDecoder("utf-8", { fatal: true }).decode(body);
        if (path.endsWith(".json")) {
          try {
            content = JSON.stringify(scrub(env, JSON.parse(content)), null, 2);
          } catch {
            content = redact(env, content);
          }
        } else content = redact(env, content);
        body = new TextEncoder().encode(content);
      }
      const key = `runs/${id}/artifacts/${path}`,
        digest = await hash(body),
        existing = await env.ARTIFACTS.head(key);
      if (existing)
        return reply(existing.customMetadata?.sha256 === digest ? 200 : 409, {
          ok: existing.customMetadata?.sha256 === digest,
        });
      if (!active(run.status))
        return reply(409, { error: "This run is already finalized." }, true);
      const saved = await env.ARTIFACTS.put(key, body, {
        onlyIf: new Headers({ "If-None-Match": "*" }),
        httpMetadata: { contentType: image || "application/octet-stream" },
        customMetadata: { sha256: digest },
      });
      if (!saved) {
        const raced = await env.ARTIFACTS.head(key);
        return reply(raced?.customMetadata?.sha256 === digest ? 200 : 409, {
          ok: raced?.customMetadata?.sha256 === digest,
        });
      }
      return reply(200, { ok: true });
    }
    const payload = JSON.parse(
      new TextDecoder().decode(await boundedBody(request, 8 * 1024 * 1024)),
    );
    if (
      !["running", "completed", "failed"].includes(payload.status) ||
      (payload.runId && payload.runId !== id)
    )
      return reply(400, { error: "Invalid callback." });
    if (!active(run.status))
      return reply(200, { ok: true, status: run.status }, true);
    if (payload.status === "running") {
      await env.DB.prepare(
        "UPDATE runs SET status='running',error=NULL,updated_at=? WHERE id=? AND status='queued'",
      )
        .bind(new Date().toISOString(), id)
        .run();
      return reply(200, { ok: true });
    }
    const candidate = scrub(
      env,
      normalizedDetail(
        run,
        payload.detail,
        payload.status === "failed",
        payload.error,
      ),
    );
    const key = `runs/${id}/detail.json`,
      encoded = JSON.stringify(candidate);
    if (byteLength(encoded) > 8 * 1024 * 1024)
      return reply(413, { error: "Result detail is too large." });
    const saved = await env.ARTIFACTS.put(key, encoded, {
      onlyIf: new Headers({ "If-None-Match": "*" }),
      httpMetadata: { contentType: "application/json" },
    });
    const detail = saved
      ? candidate
      : await (await env.ARTIFACTS.get(key))!.json<RunDetail>();
    const compact = JSON.stringify({
      score: detail.score,
      durationSec: detail.durationSec,
      costUsd: detail.costUsd,
    });
    await env.DB.prepare(
      "UPDATE runs SET status=?,detail_json=?,error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
    )
      .bind(detail.status, compact, detail.error, new Date().toISOString(), id)
      .run();
    return reply(200, { ok: true, status: detail.status }, true);
  } catch (error) {
    const status = /size|large/i.test(errorText(error))
      ? 413
      : error instanceof SyntaxError ||
          error instanceof TypeError ||
          /Invalid run detail|Completed runs require/.test(errorText(error))
        ? 400
        : 500;
    return reply(status, { error: redact(env, errorText(error)) });
  }
}

export async function artifactResponse(
  ctx: CloudContext,
  id: string,
  path: string,
): Promise<Response> {
  if (!validId(id) || !safeArtifactPath(path))
    return reply(400, { error: "Invalid artifact path." });
  if (!(await row(ctx.env, id))) return reply(404, { error: "Run not found." });
  const artifact = await ctx.env.ARTIFACTS.get(`runs/${id}/artifacts/${path}`);
  if (!artifact) return reply(404, { error: "Artifact not found." });
  const image = images[path.split(".").at(-1)!.toLowerCase()],
    filename = path
      .split("/")
      .at(-1)!
      .replace(/["\r\n]/g, "_");
  return new Response(artifact.body, {
    headers: {
      ...headers,
      "content-type": image || "application/octet-stream",
      "content-security-policy": "sandbox; default-src 'none'",
      "content-disposition": `${image ? "inline" : "attachment"}; filename*=UTF-8''${encodeURIComponent(filename)}`,
    },
  });
}

export async function expireStaleRuns(env: CloudEnv): Promise<void> {
  await env.DB.prepare(
    "UPDATE runs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running') AND created_at < ?",
  )
    .bind(
      "The runner did not report completion within 80 minutes. Its one-hour execution limit has elapsed.",
      new Date().toISOString(),
      new Date(Date.now() - 80 * 60 * 1000).toISOString(),
    )
    .run();
}
