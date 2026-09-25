import { createServerFn } from "@tanstack/react-start";
import { context } from "./cloud-context";
import * as store from "./cloud-store";
import * as runs from "./cloud-runs";
import { assistDraft as assist } from "./cloud-ai";
import { githubConfigured } from "./cloud-github-auth";
import type {
  AssistInput,
  CloudDashboard,
  CommitInput,
  SaveDraftInput,
} from "./cloud-types";
import type { Agent } from "./types";

export const dashboard = createServerFn({ method: "GET" }).handler(
  async (): Promise<CloudDashboard> => {
    const ctx = context();
    const [tasks, history] = await Promise.all([
      store.listTasks(ctx),
      runs.listRuns(ctx),
    ]);
    const e = ctx.env;
    return {
      user: ctx.user,
      tasks,
      runs: history,
      settings: {
        repository: e.GITHUB_REPOSITORY || "",
        branch: e.GITHUB_BRANCH || "main",
        gitConfigured: githubConfigured(e),
        aiConfigured: !!e.ANTHROPIC_API_KEY,
        runnerConfigured:
          e.LOCAL_DEV !== "true" &&
          !!(
            e.APP_ORIGIN &&
            e.E2B_API_KEY &&
            e.EXPO_TOKEN &&
            e.TYPESAFE_API_KEY
          ),
        claudeConfigured: !!e.ANTHROPIC_API_KEY,
        codexConfigured: !!e.OPENAI_API_KEY,
        local: e.LOCAL_DEV === "true",
      },
    };
  },
);
export const saveDraft = createServerFn({ method: "POST" })
  .validator((data: SaveDraftInput) => data)
  .handler(({ data }) => store.saveDraft(context(), data));
export const commitTask = createServerFn({ method: "POST" })
  .validator((data: CommitInput) => data)
  .handler(({ data }) => store.commitTask(context(), data));
export const assistDraft = createServerFn({ method: "POST" })
  .validator((data: AssistInput) => data)
  .handler(({ data }) => assist(context(), data));
export const startRun = createServerFn({ method: "POST" })
  .validator(
    (data: { taskId: string; revision: number; agent: Agent; model: string }) =>
      data,
  )
  .handler(({ data }) => runs.startRun(context(), data));
export const runDetail = createServerFn({ method: "GET" })
  .validator((data: string) => data)
  .handler(({ data }) => runs.getRun(context(), data));
