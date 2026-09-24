import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import {
  commitTask,
  getTask,
  listTasks,
  saveDraft,
  validateFiles,
} from "./cloud-store.ts";
import { mergeFiles, taskPaths } from "./cloud-github.ts";
import { assistDraft, validateProposal } from "./cloud-ai.ts";
import { seedTask } from "./seed-task.ts";
import type { AssistInput, CloudContext } from "./cloud-types";
import type { ConfigFile } from "./types";

function database() {
  const sqlite = new DatabaseSync(":memory:");
  sqlite.exec(
    readFileSync(
      new URL("../../migrations/0001_shared_evals.sql", import.meta.url),
      "utf8",
    ),
  );
  class Statement {
    values: any[] = [];
    sql: string;
    constructor(sql: string) {
      this.sql = sql;
    }
    bind(...values: any[]) {
      this.values = values;
      return this;
    }
    async first() {
      return sqlite.prepare(this.sql).get(...this.values) ?? null;
    }
    async all() {
      return { results: sqlite.prepare(this.sql).all(...this.values) };
    }
    runSync() {
      const result = sqlite.prepare(this.sql).run(...this.values);
      return { success: true, meta: { changes: Number(result.changes) } };
    }
    async run() {
      return this.runSync();
    }
  }
  const db = {
    prepare(sql: string) {
      return new Statement(sql);
    },
    async batch(statements: Statement[]) {
      sqlite.exec("BEGIN");
      try {
        const result = statements.map((statement) => statement.runSync());
        sqlite.exec("COMMIT");
        return result;
      } catch (error) {
        sqlite.exec("ROLLBACK");
        throw error;
      }
    },
  } as unknown as D1Database;
  const ctx = {
    user: "editor@example.com",
    env: {
      DB: db,
      GITHUB_REPOSITORY: "company/evals",
      GITHUB_BRANCH: "main",
      GITHUB_TOKEN: "fixture-github-secret",
    },
  } as CloudContext;
  return { ctx, sqlite };
}

function change(files: ConfigFile[], suffix: string, content: string) {
  return files.map((file) =>
    file.path.endsWith("/" + suffix) ? { ...file, content } : { ...file },
  );
}

function githubFixture(initial = seedTask.files) {
  const fixture = {
    head: "a".repeat(40),
    files: structuredClone(initial),
    mutations: [] as any[],
    beforeRead: undefined as (() => Promise<void>) | undefined,
    raceOnce: false,
  };
  const fetcher: typeof fetch = async (_url, init) => {
    const { query, variables } = JSON.parse(String(init?.body));
    if (query.includes("createCommitOnBranch")) {
      fixture.mutations.push(variables.input);
      if (fixture.raceOnce) {
        fixture.raceOnce = false;
        fixture.head = "b".repeat(40);
        fixture.files = change(
          fixture.files,
          "environment/setup.sh",
          "npm install -g eas-cli\necho remote change\n",
        );
        return Response.json({
          errors: [
            { message: "expectedHeadOid does not match the current head" },
          ],
        });
      }
      assert.equal(variables.input.expectedHeadOid, fixture.head);
      for (const file of variables.input.fileChanges.additions) {
        const content = Buffer.from(file.contents, "base64").toString("utf8");
        const current = fixture.files.find((f) => f.path === file.path);
        if (current) current.content = content;
        else fixture.files.push({ path: file.path, content });
      }
      fixture.head = "c".repeat(40);
      return Response.json({
        data: {
          createCommitOnBranch: {
            commit: {
              oid: fixture.head,
              url: `https://github.com/company/evals/commit/${fixture.head}`,
            },
          },
        },
      });
    }
    if (query.includes("qualifiedName")) {
      await fixture.beforeRead?.();
      return Response.json({
        data: { repository: { ref: { target: { oid: fixture.head } } } },
      });
    }
    const repository: Record<string, unknown> = {};
    taskPaths.forEach((_, i) => {
      const [head, ...pathParts] = variables[`p${i}`].split(":");
      assert.equal(head, fixture.head);
      const file = fixture.files.find(
        (file) => file.path === pathParts.join(":"),
      );
      repository[`f${i}`] = file
        ? { text: file.content, byteSize: file.content.length, isBinary: false }
        : null;
    });
    return Response.json({ data: { repository } });
  };
  return { fixture, fetcher };
}

test("shared drafts use SQL compare-and-swap and append-only revisions", async () => {
  const { ctx, sqlite } = database();
  try {
    const [task] = await listTasks(ctx);
    assert.equal((await listTasks(ctx)).length, 1);
    const first = await saveDraft(ctx, {
      taskId: task.id,
      name: task.name,
      revision: task.revision,
      files: change(task.files, "instruction.md", "Create a tea timer.\n"),
    });
    assert.equal(first.revision, 2);
    await assert.rejects(
      saveDraft(ctx, {
        taskId: task.id,
        name: task.name,
        revision: task.revision,
        files: task.files,
      }),
      /changed/,
    );
    const revisions = sqlite
      .prepare(
        "SELECT revision,created_by,action FROM task_revisions ORDER BY revision",
      )
      .all();
    assert.deepEqual(
      revisions.map((r) => [r.revision, r.action]),
      [
        [1, "create"],
        [2, "save"],
      ],
    );
    assert.equal(revisions[1].created_by, ctx.user);
    assert.equal(
      (await getTask(ctx, task.id)).files[0].content,
      "Create a tea timer.\n",
    );
  } finally {
    sqlite.close();
  }
});

test("three-way merge keeps independent files and flags conflicting edits", () => {
  const base = [
    { path: "prompt", content: "original" },
    { path: "setup", content: "install" },
  ];
  const ours = [{ path: "prompt", content: "new prompt" }, base[1]];
  const theirs = [base[0], { path: "setup", content: "new install" }];
  assert.deepEqual(mergeFiles(base, ours, theirs), {
    files: [ours[0], theirs[1]],
    conflicts: [],
  });
  const conflict = mergeFiles(base, ours, [
    { path: "prompt", content: "other prompt" },
    base[1],
  ]);
  assert.deepEqual(conflict.conflicts, [
    {
      path: "prompt",
      base: "original",
      ours: "new prompt",
      theirs: "other prompt",
    },
  ]);
  assert.equal(
    mergeFiles(base, ours, [{ path: "prompt", content: "other prompt" }], {
      prompt: "combined",
    }).files[0].content,
    "combined",
  );
});

test("commits merge remote files, reserve the draft, and use expectedHeadOid after a branch race", async () => {
  const { ctx, sqlite } = database();
  const originalFetch = globalThis.fetch;
  const { fixture, fetcher } = githubFixture();
  globalThis.fetch = fetcher;
  try {
    const [initial] = await listTasks(ctx);
    const baseline = await commitTask(ctx, {
      taskId: initial.id,
      revision: initial.revision,
      message: "records coffee timer",
    });
    assert.equal(baseline.ok, true);
    assert.equal(
      fixture.mutations.length,
      0,
      "already matching files reuse the current commit",
    );
    if (!baseline.ok) throw new Error("Expected baseline");
    const draft = await saveDraft(ctx, {
      taskId: initial.id,
      name: initial.name,
      revision: baseline.task.revision,
      files: change(
        baseline.task.files,
        "instruction.md",
        "Create a café timer.\n",
      ),
    });
    fixture.beforeRead = async () => {
      await assert.rejects(
        saveDraft(ctx, {
          taskId: initial.id,
          name: initial.name,
          revision: draft.revision,
          files: draft.files,
        }),
        /being committed/,
      );
      fixture.beforeRead = undefined;
    };
    fixture.raceOnce = true;
    const result = await commitTask(ctx, {
      taskId: initial.id,
      revision: draft.revision,
      message: "updates the timer prompt",
    });
    assert.equal(result.ok, true);
    if (!result.ok) throw new Error("Expected commit");
    assert.equal(result.task.committedRevision, result.task.revision);
    assert.equal(result.task.baseCommit, fixture.head);
    assert.equal(
      result.task.files.find((f) => f.path.endsWith("instruction.md"))?.content,
      "Create a café timer.\n",
    );
    assert.match(
      result.task.files.find((f) => f.path.endsWith("setup.sh"))!.content,
      /remote change/,
    );
    assert.equal(fixture.mutations.length, 2);
    assert.equal(fixture.mutations[1].expectedHeadOid, "b".repeat(40));
    assert.equal(
      sqlite.prepare("SELECT commit_token FROM tasks").get()!.commit_token,
      null,
    );
  } finally {
    globalThis.fetch = originalFetch;
    sqlite.close();
  }
});

test("conflict resolutions cannot silently override a newer remote head", async () => {
  const { ctx, sqlite } = database();
  const originalFetch = globalThis.fetch;
  const { fixture, fetcher } = githubFixture();
  globalThis.fetch = fetcher;
  try {
    const [task] = await listTasks(ctx);
    fixture.files = change(fixture.files, "instruction.md", "Remote prompt.");
    const conflict = await commitTask(ctx, {
      taskId: task.id,
      revision: task.revision,
      message: "adds timer task",
    });
    assert.equal(conflict.ok, false);
    if (conflict.ok) throw new Error("Expected conflict");
    assert.equal(fixture.mutations.length, 0);
    fixture.head = "d".repeat(40);
    fixture.files = change(
      fixture.files,
      "instruction.md",
      "Newer remote prompt.",
    );
    const stale = await commitTask(ctx, {
      taskId: task.id,
      revision: task.revision,
      message: "resolves timer prompt",
      expectedHead: conflict.head,
      resolutions: { [`tasks/${task.id}/instruction.md`]: "My resolution." },
    });
    assert.equal(stale.ok, false);
    if (stale.ok) throw new Error("Expected newer conflict");
    assert.equal(stale.conflicts[0].theirs, "Newer remote prompt.");
    assert.equal(fixture.mutations.length, 0);
    const resolved = await commitTask(ctx, {
      taskId: task.id,
      revision: task.revision,
      message: "resolves timer prompt",
      expectedHead: stale.head,
      resolutions: { [`tasks/${task.id}/instruction.md`]: "Combined prompt." },
    });
    assert.equal(resolved.ok, true);
    assert.equal(fixture.mutations.length, 1);
  } finally {
    globalThis.fetch = originalFetch;
    sqlite.close();
  }
});

test("rejects path traversal, malformed verifier TOML, embedded credentials, and invalid commit messages", async () => {
  const { ctx, sqlite } = database();
  try {
    assert.throws(() => validateFiles("../escape", seedTask.files), /task ID/);
    assert.throws(
      () =>
        validateFiles(seedTask.id, [
          ...seedTask.files.slice(1),
          { path: ".env", content: "key=x" },
        ]),
      /path/,
    );
    assert.throws(
      () =>
        validateFiles(
          seedTask.id,
          change(seedTask.files, "tests/rubric.toml", "[judge"),
        ),
      /TOML|Unexpected|parse|Expected/i,
    );
    assert.throws(
      () =>
        validateFiles(
          seedTask.id,
          change(seedTask.files, "instruction.md", "fixture-github-secret"),
          ctx.env,
        ),
      /secret/,
    );
    const [task] = await listTasks(ctx);
    await assert.rejects(
      commitTask(ctx, {
        taskId: task.id,
        revision: task.revision,
        message: "adds fixture-github-secret",
      }),
      /secret/,
    );
    for (const message of [
      "Fix timer",
      "fix: timer",
      "fix\ntimer",
      "Co-Authored-By: User",
    ])
      await assert.rejects(
        commitTask(ctx, { taskId: task.id, revision: task.revision, message }),
        /single-line/,
      );
  } finally {
    sqlite.close();
  }
});

test("does not return configured secrets in Git conflict contents", async () => {
  const { ctx, sqlite } = database();
  const originalFetch = globalThis.fetch;
  const { fixture, fetcher } = githubFixture();
  globalThis.fetch = fetcher;
  try {
    const [task] = await listTasks(ctx);
    fixture.files = change(
      fixture.files,
      "instruction.md",
      "accidentally contains fixture-github-secret",
    );
    await assert.rejects(
      commitTask(ctx, {
        taskId: task.id,
        revision: task.revision,
        message: "adds timer task",
      }),
      (error) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /secret/);
        assert.ok(!error.message.includes("fixture-github-secret"));
        return true;
      },
    );
    assert.equal(fixture.mutations.length, 0);
    assert.equal(
      sqlite.prepare("SELECT commit_token FROM tasks").get()!.commit_token,
      null,
    );
  } finally {
    globalThis.fetch = originalFetch;
    sqlite.close();
  }
});

test("assistant sends only task context and returns a reviewable proposal without writing it", async () => {
  const { ctx, sqlite } = database();
  const originalFetch = globalThis.fetch;
  ctx.env.ANTHROPIC_API_KEY = "fixture-anthropic-secret";
  try {
    const [task] = await listTasks(ctx);
    const input: AssistInput = {
      taskId: task.id,
      field: "prompt",
      files: task.files,
      instruction: "Make the prompt more precise.",
    };
    const proposal = {
      explanation: "Makes the output explicit.",
      files: [
        {
          path: `tasks/${task.id}/instruction.md`,
          content: "Create a timer and download a screenshot.",
        },
      ],
    };
    globalThis.fetch = async (url, options) => {
      assert.equal(url, "https://api.anthropic.com/v1/messages");
      const request = JSON.parse(String(options?.body));
      assert.equal(request.model, "claude-opus-5-5");
      assert.deepEqual(request.tool_choice, { type: "auto" });
      assert.match(request.system, /propose_task_files exactly once/);
      assert.ok(!String(options?.body).includes("fixture-anthropic-secret"));
      assert.ok(!String(options?.body).includes("fixture-github-secret"));
      return Response.json({
        content: [
          { type: "tool_use", name: "propose_task_files", input: proposal },
        ],
      });
    };
    assert.deepEqual(await assistDraft(ctx, input), proposal);
    assert.deepEqual(
      await assistDraft(ctx, {
        ...input,
        files: change(input.files, "instruction.md", ""),
      }),
      proposal,
      "assistant can write the prompt for a new task",
    );
    assert.deepEqual(
      (await getTask(ctx, task.id)).files,
      task.files,
      "assistant does not save the proposed edit",
    );
    globalThis.fetch = async () =>
      Response.json(
        {
          error: {
            message: "Unsupported model option; fixture-anthropic-secret",
          },
        },
        { status: 400 },
      );
    await assert.rejects(assistDraft(ctx, input), (error) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /400.*Unsupported model option/);
      assert.ok(!error.message.includes("fixture-anthropic-secret"));
      assert.match(error.message, /\[redacted\]/);
      return true;
    });
    assert.throws(
      () =>
        validateProposal(
          input,
          {
            ...proposal,
            files: [
              {
                path: `tasks/${task.id}/environment/setup.sh`,
                content: "echo changed",
              },
            ],
          },
          ctx,
        ),
      /outside/,
    );
    assert.throws(
      () =>
        validateProposal(
          input,
          {
            ...proposal,
            files: [
              {
                path: `tasks/${task.id}/instruction.md`,
                content: "fixture-anthropic-secret",
              },
            ],
          },
          ctx,
        ),
      /secret/,
    );
  } finally {
    globalThis.fetch = originalFetch;
    sqlite.close();
  }
});
