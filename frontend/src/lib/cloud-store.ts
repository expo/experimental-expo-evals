import { parse } from "smol-toml";
import type {
  CloudContext,
  CloudEnv,
  CommitInput,
  CommitResult,
  SaveDraftInput,
  TaskDocument,
} from "./cloud-types";
import type { ConfigFile } from "./types";
import { seedTask } from "./seed-task.ts";
import {
  createTaskCommit,
  isStaleHead,
  mergeFiles,
  readRemoteTask,
  taskPaths,
  validateTaskId,
} from "./cloud-github.ts";

type TaskRow = {
  id: string;
  name: string;
  revision: number;
  base_commit: string | null;
  committed_revision: number | null;
  files_json: string;
  base_files_json: string;
  updated_at: string;
  updated_by: string;
  commit_token: string | null;
  commit_expires: number | null;
};

function rejectSecretValues(env: CloudEnv | undefined, content: string) {
  const secrets = env
    ? [
        env.GITHUB_TOKEN,
        env.GITHUB_APP_PRIVATE_KEY,
        env.DASHBOARD_PASSWORD,
        env.SESSION_SECRET,
        env.ANTHROPIC_API_KEY,
        env.E2B_API_KEY,
        env.EXPO_TOKEN,
        env.TYPESAFE_API_KEY,
        env.OPENAI_API_KEY,
      ].filter((secret): secret is string => !!secret && secret.length >= 8)
    : [];
  if (secrets.some((secret) => content.includes(secret))) {
    throw new Error(
      "Task changes contain configured secret values. Remove them and use environment variable references.",
    );
  }
}

export function validateFiles(
  id: string,
  value: unknown,
  env?: CloudEnv,
  options: { allowEmptyPrompt?: boolean } = {},
): ConfigFile[] {
  validateTaskId(id);
  if (!Array.isArray(value) || value.length !== taskPaths.length)
    throw new Error("Provide all six native task files.");
  const allowed = new Set(taskPaths.map((path) => `tasks/${id}/${path}`));
  let total = 0;
  const files: ConfigFile[] = value.map((file) => {
    if (
      !file ||
      typeof file.path !== "string" ||
      !allowed.delete(file.path) ||
      typeof file.content !== "string" ||
      file.content.includes("\0")
    )
      throw new Error("Unsupported or duplicate task file path.");
    const size = new TextEncoder().encode(file.content).length;
    total += size;
    if (size > 100_000 || total > 300_000)
      throw new Error("Task files exceed the size limit.");
    rejectSecretValues(env, file.content);
    return { path: file.path, content: file.content };
  });
  const text = (suffix: string) =>
    files.find((f) => f.path === `tasks/${id}/${suffix}`)!.content;
  if (!text("instruction.md").trim() && !options.allowEmptyPrompt)
    throw new Error("The task prompt cannot be empty.");
  const rubric = parse(text("tests/rubric.toml")) as Record<string, any>;
  if (
    rubric.judge?.judge !== "jev" ||
    !Array.isArray(rubric.judge?.files) ||
    !rubric.judge.files.length
  )
    throw new Error(
      "The verifier must configure the JEV judge and its input files.",
    );
  if (
    !Array.isArray(rubric.criterion) ||
    !rubric.criterion.length ||
    rubric.criterion.length > 50
  )
    throw new Error("Add between one and fifty verifier criteria.");
  const names = new Set<string>();
  for (const criterion of rubric.criterion) {
    if (
      !criterion ||
      typeof criterion.name !== "string" ||
      !criterion.name.trim() ||
      names.has(criterion.name) ||
      typeof criterion.description !== "string" ||
      !criterion.description.trim() ||
      criterion.type !== "binary"
    )
      throw new Error(
        "Each criterion needs a unique name, description, and binary type.",
      );
    names.add(criterion.name);
  }
  if (
    ![
      "all-pass",
      "any-pass",
      "required-pass",
      "threshold",
      "weighted-mean",
      "weighted-sum",
    ].includes(
      String(rubric.scoring?.aggregation ?? "weighted-mean").replaceAll(
        "_",
        "-",
      ),
    )
  )
    throw new Error("Use a supported RewardKit scoring aggregation.");
  const task = parse(text("task.toml")) as Record<string, any>;
  if (!task.schema_version)
    throw new Error("task.toml must declare its Harbor schema_version.");
  if (
    !text("tests/test.sh").trim() ||
    !text("environment/setup.sh").trim() ||
    !text("environment/Dockerfile").trim()
  )
    throw new Error("Setup, Dockerfile, and verifier scripts cannot be empty.");
  return files;
}

function document(row: TaskRow): TaskDocument {
  return {
    id: row.id,
    name: row.name,
    revision: row.revision,
    baseCommit: row.base_commit,
    committedRevision: row.committed_revision,
    files: JSON.parse(row.files_json),
    updatedAt: row.updated_at,
    updatedBy: row.updated_by,
  };
}

async function row(ctx: CloudContext, id: string) {
  validateTaskId(id);
  return ctx.env.DB.prepare("SELECT * FROM tasks WHERE id = ?")
    .bind(id)
    .first<TaskRow>();
}

function history(db: D1Database, id: string, change: string, action: string) {
  return db
    .prepare(
      `INSERT INTO task_revisions (task_id,revision,name,files_json,base_commit,action,created_at,created_by)
    SELECT id,revision,name,files_json,base_commit,?,updated_at,updated_by FROM tasks WHERE id=? AND change_id=?`,
    )
    .bind(action, id, change);
}

async function seed(ctx: CloudContext) {
  const change = crypto.randomUUID();
  await ctx.env.DB.batch([
    ctx.env.DB.prepare(
      `INSERT OR IGNORE INTO tasks (id,name,revision,files_json,updated_at,updated_by,change_id) VALUES (?,?,1,?,?,?,?)`,
    ).bind(
      seedTask.id,
      seedTask.name,
      JSON.stringify(seedTask.files),
      new Date().toISOString(),
      "Initial example",
      change,
    ),
    history(ctx.env.DB, seedTask.id, change, "create"),
  ]);
}

export async function listTasks(ctx: CloudContext): Promise<TaskDocument[]> {
  await seed(ctx);
  const result = await ctx.env.DB.prepare(
    "SELECT * FROM tasks ORDER BY name, id",
  ).all<TaskRow>();
  return result.results.map(document);
}

export async function getTask(
  ctx: CloudContext,
  id: string,
): Promise<TaskDocument> {
  await seed(ctx);
  const found = await row(ctx, id);
  if (!found) throw new Error("Task not found.");
  return document(found);
}

export async function saveDraft(
  ctx: CloudContext,
  input: SaveDraftInput,
): Promise<TaskDocument> {
  validateTaskId(input.taskId);
  if (
    typeof input.name !== "string" ||
    !input.name.trim() ||
    input.name.length > 100
  )
    throw new Error("Provide a task name up to 100 characters.");
  if (
    input.revision !== null &&
    (!Number.isInteger(input.revision) || input.revision < 1)
  )
    throw new Error("Invalid draft revision.");
  const files = validateFiles(input.taskId, input.files, ctx.env);
  const change = crypto.randomUUID(),
    now = new Date().toISOString();
  const statement =
    input.revision === null
      ? ctx.env.DB.prepare(
          `INSERT OR IGNORE INTO tasks (id,name,revision,files_json,updated_at,updated_by,change_id) VALUES (?,?,1,?,?,?,?)`,
        ).bind(
          input.taskId,
          input.name.trim(),
          JSON.stringify(files),
          now,
          ctx.user,
          change,
        )
      : ctx.env.DB.prepare(
          `UPDATE tasks SET name=?,files_json=?,revision=revision+1,updated_at=?,updated_by=?,change_id=?,commit_token=NULL,commit_expires=NULL
        WHERE id=? AND revision=? AND (commit_token IS NULL OR commit_expires < ?)`,
        ).bind(
          input.name.trim(),
          JSON.stringify(files),
          now,
          ctx.user,
          change,
          input.taskId,
          input.revision,
          Date.now(),
        );
  const results = await ctx.env.DB.batch([
    statement,
    history(
      ctx.env.DB,
      input.taskId,
      change,
      input.revision === null ? "create" : "save",
    ),
  ]);
  if (!results[0].meta.changes)
    throw new Error(
      "This draft changed or is being committed by someone else. Reload the latest draft before saving. Your edits have not been overwritten.",
    );
  return document((await row(ctx, input.taskId))!);
}

function validateMessage(message: string) {
  if (
    typeof message !== "string" ||
    !message.trim() ||
    message.length > 160 ||
    /[\r\n]/.test(message) ||
    /^[A-Z]/.test(message.trim()) ||
    /^(?:fix|feat|chore|docs|refactor|test|style|perf|build|ci)(?:\([^)]*\))?:/i.test(
      message.trim(),
    ) ||
    /co-authored-by/i.test(message)
  ) {
    throw new Error(
      "Use a single-line commit message starting with lowercase text, without a conventional commit prefix.",
    );
  }
  return message.trim();
}

export async function commitTask(
  ctx: CloudContext,
  input: CommitInput,
): Promise<CommitResult> {
  validateTaskId(input.taskId);
  const message = validateMessage(input.message);
  rejectSecretValues(ctx.env, message);
  if (!Number.isInteger(input.revision) || input.revision < 1)
    throw new Error("Invalid draft revision.");
  if (
    input.resolutions &&
    (!input.expectedHead || typeof input.resolutions !== "object")
  )
    throw new Error(
      "Conflict resolutions must include the remote commit they resolve.",
    );
  const prefix = `tasks/${input.taskId}/`;
  for (const [path, content] of Object.entries(input.resolutions ?? {})) {
    if (
      !taskPaths.some((suffix) => path === prefix + suffix) ||
      typeof content !== "string" ||
      new TextEncoder().encode(content).length > 100_000
    )
      throw new Error("Invalid conflict resolution file.");
  }
  const token = crypto.randomUUID();
  const claim = await ctx.env.DB.prepare(
    `UPDATE tasks SET commit_token=?,commit_expires=? WHERE id=? AND revision=? AND (commit_token IS NULL OR commit_expires < ?)`,
  )
    .bind(token, Date.now() + 300_000, input.taskId, input.revision, Date.now())
    .run();
  if (!claim.meta.changes)
    throw new Error(
      "This draft changed or another commit is in progress. Reload before committing.",
    );
  try {
    const draft = (await row(ctx, input.taskId))!;
    const ours = validateFiles(
      input.taskId,
      JSON.parse(draft.files_json),
      ctx.env,
    );
    const base = JSON.parse(draft.base_files_json) as ConfigFile[];
    for (let attempt = 0; attempt < 3; attempt++) {
      const remote = await readRemoteTask(ctx.env, input.taskId);
      // Conflicts are returned to browsers, so validate remote content before
      // returning a conflict rather than only validating a successful merge.
      for (const file of remote.files)
        rejectSecretValues(ctx.env, file.content);
      const staleResolution =
        !!input.resolutions && input.expectedHead !== remote.head;
      const merged = mergeFiles(
        base,
        ours,
        remote.files,
        staleResolution ? {} : input.resolutions,
      );
      if (merged.conflicts.length || staleResolution)
        return {
          ok: false,
          kind: "conflict",
          head: remote.head,
          conflicts: merged.conflicts,
          message: staleResolution
            ? "The branch changed again. Review these conflicts against the latest version before committing."
            : "Both the draft and Git changed the same files. Choose or edit the contents to keep.",
        };
      validateFiles(input.taskId, merged.files, ctx.env);
      const current = await row(ctx, input.taskId);
      if (
        current?.commit_token !== token ||
        current.revision !== input.revision ||
        (current.commit_expires ?? 0) < Date.now()
      )
        throw new Error(
          "The commit reservation expired. Reload before committing again.",
        );
      let commit;
      try {
        commit = await createTaskCommit(
          ctx.env,
          remote.head,
          merged.files,
          remote.files,
          message,
        );
      } catch (error) {
        if (isStaleHead(error) && attempt < 2) continue;
        throw error;
      }
      const change = crypto.randomUUID();
      const saved = await ctx.env.DB.batch([
        ctx.env.DB.prepare(
          `UPDATE tasks SET revision=revision+1,committed_revision=revision+1,base_commit=?,base_files_json=?,files_json=?,updated_at=?,updated_by=?,change_id=?,commit_token=NULL,commit_expires=NULL WHERE id=? AND revision=? AND commit_token=?`,
        ).bind(
          commit.sha,
          JSON.stringify(merged.files),
          JSON.stringify(merged.files),
          new Date().toISOString(),
          ctx.user,
          change,
          input.taskId,
          input.revision,
          token,
        ),
        history(ctx.env.DB, input.taskId, change, "commit"),
      ]);
      if (!saved[0].meta.changes)
        throw new Error(
          `Git commit ${commit.sha} was created, but the draft changed before it could be recorded. Reload and commit again to reconcile.`,
        );
      return {
        ok: true,
        task: document((await row(ctx, input.taskId))!),
        ...commit,
      };
    }
    throw new Error(
      "The branch changed repeatedly. Reload and try the commit again.",
    );
  } finally {
    await ctx.env.DB.prepare(
      "UPDATE tasks SET commit_token=NULL,commit_expires=NULL WHERE id=? AND commit_token=?",
    )
      .bind(input.taskId, token)
      .run();
  }
}
