import type { Agent, ConfigFile, RunDetail, RunSummary } from "./types";

export type TaskDocument = {
  id: string;
  name: string;
  revision: number;
  baseCommit: string | null;
  committedRevision: number | null;
  files: ConfigFile[];
  updatedAt: string;
  updatedBy: string;
};
export type SaveDraftInput = {
  taskId: string;
  name: string;
  revision: number | null;
  files: ConfigFile[];
};
export type CommitInput = {
  taskId: string;
  revision: number;
  message: string;
  expectedHead?: string;
  resolutions?: Record<string, string>;
};
export type CommitResult =
  | { ok: true; task: TaskDocument; sha: string; url: string }
  | {
      ok: false;
      kind: "conflict";
      head: string;
      message: string;
      conflicts: { path: string; base: string; ours: string; theirs: string }[];
    };
export type AssistInput = {
  taskId: string;
  files: ConfigFile[];
  field: "prompt" | "setup" | "verifier";
  instruction: string;
};
export type AssistResult = { files: ConfigFile[]; explanation: string };
export type CloudDashboard = {
  tasks: TaskDocument[];
  runs: (RunSummary & { commitSha: string | null })[];
  settings: {
    repository: string;
    gitConfigured: boolean;
    aiConfigured: boolean;
    runnerConfigured: boolean;
    claudeConfigured: boolean;
    codexConfigured: boolean;
    local: boolean;
  };
};
export type CloudEnv = {
  DB: D1Database;
  ARTIFACTS: R2Bucket;
  RUNNERS: DurableObjectNamespace;
  GITHUB_REPOSITORY?: string;
  GITHUB_BRANCH?: string;
  GITHUB_TOKEN?: string;
  GITHUB_APP_ID?: string;
  GITHUB_APP_INSTALLATION_ID?: string;
  GITHUB_APP_PRIVATE_KEY?: string;
  ANTHROPIC_API_KEY?: string;
  ASSIST_MODEL?: string;
  E2B_API_KEY?: string;
  EXPO_TOKEN?: string;
  TYPESAFE_API_KEY?: string;
  OPENAI_API_KEY?: string;
  APP_ORIGIN?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  DASHBOARD_USERNAME?: string;
  DASHBOARD_PASSWORD?: string;
  SESSION_SECRET?: string;
  LOCAL_DEV?: string;
};
export type CloudContext = { env: CloudEnv; user: string };
export type RunnerPayload = {
  runId: string;
  taskId: string;
  commitSha: string;
  files: ConfigFile[];
  agent: Agent;
  model: string;
};
export type RunnerCallback = {
  status: "running" | "completed" | "failed";
  detail?: RunDetail;
  error?: string;
};
