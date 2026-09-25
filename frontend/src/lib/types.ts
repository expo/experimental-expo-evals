export type Agent = "claude-code" | "codex";
export type ConfigFile = { path: string; content: string };
export type RunSummary = {
  id: string;
  task: string;
  agent: string;
  model: string;
  startedAt: string;
  status: "queued" | "running" | "passed" | "failed" | "error" | "interrupted";
  score: number | null;
  durationSec: number | null;
  costUsd: number | null;
};
export type RunDetail = RunSummary & {
  criteria: {
    name: string;
    description: string;
    score: number;
    probability: number | null;
  }[];
  steps: { id: string; role: string; text: string }[];
  log: string;
  artifacts: { path: string; name: string; image: boolean }[];
  error: string | null;
  configFiles: ConfigFile[];
};
