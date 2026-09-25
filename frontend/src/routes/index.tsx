import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import {
  ArrowDownToLine,
  ArrowUpRight,
  Check,
  ChevronRight,
  Circle,
  Clock3,
  Coffee,
  FileText,
  FlaskConical,
  LoaderCircle,
  Plus,
  Terminal,
  X,
} from "lucide-react";
import { dashboard, runDetail } from "../lib/cloud.functions";
import type { ConfigFile, RunDetail, RunSummary } from "../lib/types";
import { TaskEditor } from "../components/TaskEditor";

export const Route = createFileRoute("/")({
  loader: () => dashboard(),
  validateSearch: (
    search: Record<string, unknown>,
  ): { run?: string; task?: string } => ({
    run: typeof search.run === "string" ? search.run : undefined,
    task: typeof search.task === "string" ? search.task : undefined,
  }),
  component: App,
});

const title = (value: string) =>
  value.replaceAll("-", " ").replace(/^./, (c) => c.toUpperCase());
const duration = (value: number | null) =>
  value == null ? "—" : `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
const money = (value: number | null) =>
  value == null ? "—" : `$${value.toFixed(2)}`;
const date = (value: string) =>
  value
    ? new Date(value).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Starting…";
const artifactUrl = (id: string, path: string) =>
  `/api/artifact?${new URLSearchParams({ job: id, path })}`;
const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";

function App() {
  const initial = Route.useLoaderData();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [data, setData] = useState(initial);
  const [view, setView] = useState<"tasks" | "runs">(
    search.run ? "runs" : "tasks",
  );
  const [taskId, setTaskId] = useState<string | null>(
    search.task ?? initial.tasks[0]?.id ?? null,
  );
  const [runId, setRunId] = useState<string | null>(
    search.run ?? initial.runs[0]?.id ?? null,
  );
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [mutationPending, setMutationPending] = useState(false);

  async function refresh() {
    const next = await dashboard();
    setData((current) => ({
      ...next,
      tasks: [
        ...next.tasks.map((incoming) => {
          const existing = current.tasks.find(
            (item) => item.id === incoming.id,
          );
          return existing && existing.revision > incoming.revision
            ? existing
            : incoming;
        }),
        ...current.tasks.filter(
          (item) => !next.tasks.some((incoming) => incoming.id === item.id),
        ),
      ],
    }));
    return next;
  }

  useEffect(() => {
    const timer = setInterval(() => {
      void refresh()
        .then(() => setRefreshError(""))
        .catch(() => setRefreshError("Connection lost. Trying again…"));
    }, 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!dirty && !mutationPending) return;
    const protect = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, mutationPending]);

  useEffect(() => {
    if (view !== "runs" || !runId) return;
    let cancelled = false;
    setDetail(null);
    setDetailError("");
    const load = () =>
      runDetail({ data: runId })
        .then((result) => {
          if (!cancelled) {
            setDetail(result);
            setDetailError("");
          }
        })
        .catch((error) => {
          if (!cancelled) setDetailError(errorText(error));
        });
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId, view]);

  function switchView(next: "tasks" | "runs") {
    if (next === view || mutationPending) return;
    if (dirty && !window.confirm("Discard your unsaved changes?")) return;
    setDirty(false);
    setView(next);
    void navigate({
      search:
        next === "runs"
          ? { run: runId ?? undefined }
          : { task: taskId ?? undefined },
      replace: true,
    });
  }

  function selectTask(id: string | null) {
    if (id === taskId || mutationPending) return;
    if (dirty && !window.confirm("Discard your unsaved changes?")) return;
    setDirty(false);
    setTaskId(id);
    void navigate({ search: { task: id ?? undefined }, replace: true });
  }

  function selectRun(id: string) {
    setRunId(id);
    void navigate({ search: { run: id }, replace: true });
  }

  const task = data.tasks.find((item) => item.id === taskId);
  const activeRun = data.runs.find(
    (run) => run.status === "queued" || run.status === "running",
  );
  const completed = data.runs.filter((run) =>
    ["passed", "failed"].includes(run.status),
  ).length;

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="/" aria-label="EAS evals home">
          <span className="brand-icon">
            <FlaskConical size={19} />
          </span>
          <strong>EAS</strong>
          <span className="brand-divider">/</span>
          <span>evals</span>
        </a>
        <span className="local">
          <span className="dot" />
          {data.settings.local
            ? "Local Cloudflare preview"
            : "Company workspace"}
        </span>
      </header>

      <main className="workspace">
        <div className="page-heading">
          <div>
            <p className="eyebrow">EAS SIMULATOR</p>
            <h1>Agent evaluations</h1>
            <p className="subtitle">
              Write tasks, commit changes, and compare agent runs.
            </p>
          </div>
          <span className="powered">
            Powered by Harbor
            <ArrowUpRight size={14} />
          </span>
        </div>
        <div className="workspace-context">
          <span>
            <span className="dot" />{" "}
            {data.settings.local ? "Local D1 database" : "Shared D1 database"}
          </span>
          <span>
            {data.settings.repository || "Git repository not connected"}{" "}
            <span className="context-divider">/</span> {data.settings.branch}
          </span>
          <span className="workspace-user">{data.user}</span>
          {!data.settings.local && (
            <form action="/logout" method="post">
              <button className="button" disabled={mutationPending}>
                Sign out
              </button>
            </form>
          )}
        </div>
        <nav className="tabs" aria-label="Workspace sections">
          <button
            className={view === "tasks" ? "active" : ""}
            disabled={mutationPending}
            onClick={() => switchView("tasks")}
          >
            <FileText size={16} />
            Tasks<span className="count">{data.tasks.length}</span>
          </button>
          <button
            className={view === "runs" ? "active" : ""}
            disabled={mutationPending}
            onClick={() => switchView("runs")}
          >
            <Terminal size={16} />
            Runs<span className="count">{data.runs.length}</span>
            {activeRun && <span className="dot running-dot" />}
          </button>
          <span className="tabs-note">
            {activeRun
              ? activeRun.status === "queued"
                ? "An eval is queued"
                : "An eval is running"
              : `${completed} completed ${completed === 1 ? "run" : "runs"}`}
          </span>
        </nav>
        {refreshError && (
          <div className="notice" role="status">
            {refreshError}
          </div>
        )}

        {view === "tasks" ? (
          <div className="task-layout">
            <aside className="task-sidebar">
              <div className="sidebar-heading">
                <span>YOUR TASKS</span>
                <button
                  className="icon-button"
                  title="New task"
                  aria-label="New task"
                  disabled={mutationPending}
                  onClick={() => selectTask(null)}
                >
                  <Plus size={17} />
                </button>
              </div>
              {data.tasks.map((item) => (
                <button
                  key={item.id}
                  className={`task-item ${item.id === taskId ? "selected" : ""}`}
                  disabled={mutationPending}
                  onClick={() => selectTask(item.id)}
                >
                  <span className="task-symbol">
                    {item.id.includes("coffee") ? (
                      <Coffee size={17} />
                    ) : (
                      <FileText size={17} />
                    )}
                  </span>
                  <span>
                    <strong>{item.name}</strong>
                    <small>
                      {item.committedRevision === item.revision &&
                      item.baseCommit
                        ? "Committed"
                        : "Draft"}{" "}
                      · revision {item.revision}
                    </small>
                  </span>
                  <ChevronRight size={14} />
                </button>
              ))}
              {taskId === null && (
                <div className="task-item selected">
                  <span className="task-symbol">
                    <Plus size={17} />
                  </span>
                  <span>
                    <strong>New task</strong>
                    <small>Draft</small>
                  </span>
                </div>
              )}
              <p className="sidebar-footnote">
                Save drafts to D1.
                <br />
                Commit native files to Git.
              </p>
            </aside>
            <TaskEditor
              key={taskId ?? "new"}
              task={task}
              data={data}
              onDirty={setDirty}
              onBusy={setMutationPending}
              onSaved={async (saved) => {
                setData((current) => ({
                  ...current,
                  tasks: [
                    saved,
                    ...current.tasks.filter((item) => item.id !== saved.id),
                  ],
                }));
                setTaskId(saved.id);
                setDirty(false);
                void navigate({ search: { task: saved.id }, replace: true });
              }}
              onStarted={async (id) => {
                await refresh();
                setDirty(false);
                setRunId(id);
                setView("runs");
                void navigate({ search: { run: id }, replace: true });
              }}
            />
          </div>
        ) : (
          <div className="runs-layout">
            <div className="runs-sidebar">
              <div className="sidebar-heading">
                <span>RUN HISTORY</span>
                <span>{data.runs.length}</span>
              </div>
              {data.runs.length === 0 ? (
                <div className="empty compact">
                  <Terminal size={24} />
                  <p>Your first run will appear here.</p>
                </div>
              ) : (
                data.runs.map((run) => (
                  <button
                    key={run.id}
                    className={`run-item ${run.id === runId ? "selected" : ""}`}
                    onClick={() => selectRun(run.id)}
                  >
                    <div>
                      <strong>{title(run.task)}</strong>
                      <Status status={run.status} small />
                    </div>
                    <span className="run-model">
                      {run.model?.split("/").pop() || run.agent}
                    </span>
                    <div className="run-meta">
                      <span>{date(run.startedAt)}</span>
                      <span>{duration(run.durationSec)}</span>
                    </div>
                  </button>
                ))
              )}
            </div>
            <section className="run-detail" aria-label="Run details">
              {detailError ? (
                <div className="notice error" role="alert">
                  {detailError}
                </div>
              ) : detail ? (
                <RunResults
                  run={detail}
                  commitSha={
                    data.runs.find((run) => run.id === detail.id)?.commitSha ??
                    null
                  }
                  repository={data.settings.repository}
                />
              ) : (
                <div className="empty">
                  <LoaderCircle className={runId ? "spin" : ""} size={24} />
                  <p>
                    {runId ? "Loading run…" : "Run a task to see its results."}
                  </p>
                </div>
              )}
            </section>
          </div>
        )}
        <footer className="footer">
          <span>D1 drafts · Git task versions · R2 results</span>
          <span>E2B environment</span>
        </footer>
      </main>
    </div>
  );
}

function Status({
  status,
  small = false,
}: {
  status: RunSummary["status"];
  small?: boolean;
}) {
  const Icon =
    status === "passed"
      ? Check
      : status === "queued"
        ? Clock3
        : status === "running"
          ? LoaderCircle
          : status === "failed" || status === "error"
            ? X
            : Circle;
  return (
    <span className={`badge ${status} ${small ? "small" : ""}`}>
      <Icon size={13} className={status === "running" ? "spin" : ""} />
      {title(status)}
    </span>
  );
}

function RunResults({
  run,
  commitSha,
  repository,
}: {
  run: RunDetail;
  commitSha: string | null;
  repository: string;
}) {
  const [tab, setTab] = useState<
    "scores" | "trace" | "config" | "files" | "log"
  >("scores");
  const screenshot = run.artifacts.find(
    (file) =>
      file.image && /(?:screenshot|coffee-timer|screen[._-])/i.test(file.name),
  );
  useEffect(() => setTab("scores"), [run.id]);
  return (
    <>
      <div className="section-heading">
        <div>
          <p className="eyebrow">EVAL RESULT</p>
          <h2>{title(run.task)}</h2>
        </div>
        <Status status={run.status} />
      </div>
      <p className="result-model">
        {run.agent} <span>/</span> {run.model}
        <br />
        <small>{date(run.startedAt)}</small>
        {commitSha && (
          <>
            <span>/</span>
            <a
              className="commit-sha"
              href={`https://github.com/${repository}/commit/${commitSha}`}
              target="_blank"
              rel="noreferrer"
              title="Open the task commit for this run"
            >
              {commitSha.slice(0, 7)}
            </a>
          </>
        )}
      </p>
      <div className="metrics">
        <div>
          <span>Overall score</span>
          <strong>
            {run.score == null ? "—" : `${Math.round(run.score * 100)}%`}
          </strong>
        </div>
        <div>
          <span>Duration</span>
          <strong>{duration(run.durationSec)}</strong>
        </div>
        <div>
          <span>Agent cost</span>
          <strong>{money(run.costUsd)}</strong>
        </div>
      </div>
      {run.error && (
        <div className="notice error" role="alert">
          {run.error}
        </div>
      )}
      <nav className="result-tabs" aria-label="Run sections">
        {(["scores", "trace", "config", "files", "log"] as const).map(
          (item) => (
            <button
              key={item}
              onClick={() => setTab(item)}
              className={tab === item ? "active" : ""}
            >
              {item === "trace"
                ? "Agent trace"
                : item === "config"
                  ? "Configuration"
                  : title(item)}
              {item === "files" && (
                <span className="count">{run.artifacts.length}</span>
              )}
            </button>
          ),
        )}
      </nav>
      {tab === "config" && (
        <div className="task-config">
          <p className="config-explanation">
            Recorded configuration for this run, with secret values redacted.
            These are historical files, not today’s task settings.
          </p>
          {run.steps[0]?.role === "user" && (
            <details className="config-file" open>
              <summary>
                <FileText size={14} />
                <span>Task prompt recorded in the agent trace</span>
                <ChevronRight size={14} />
              </summary>
              <pre>{run.steps[0].text}</pre>
            </details>
          )}
          <p className="config-explanation">
            This run keeps a copy of the committed task files and its run
            configuration.
          </p>
          <ConfigFiles files={run.configFiles} />
        </div>
      )}
      {tab === "scores" && (
        <div className="score-content">
          {run.criteria.length ? (
            <>
              <div className="criteria-heading">
                <span>CRITERION</span>
                <span>JEV PROBABILITY</span>
              </div>
              {run.criteria.map((criterion) => (
                <div className="criterion" key={criterion.name}>
                  <span
                    className={`criterion-icon ${criterion.score >= 1 ? "pass" : "fail"}`}
                  >
                    {criterion.score >= 1 ? (
                      <Check size={15} />
                    ) : (
                      <X size={15} />
                    )}
                  </span>
                  <div>
                    <strong>
                      {title(criterion.name.replaceAll("_", "-"))}
                    </strong>
                    <p>{criterion.description}</p>
                  </div>
                  <span className="probability">
                    {criterion.probability == null
                      ? "—"
                      : `${Math.round(criterion.probability * 100)}%`}
                  </span>
                </div>
              ))}
              <p className="score-note">
                Scores use this run’s rubric and aggregation settings. JEV
                assesses the agent trace; its score does not independently
                verify visual or interactive correctness.
              </p>
            </>
          ) : (
            <div className="empty compact">
              <Circle size={23} />
              <p>
                {run.status === "queued"
                  ? "Queued. Waiting for the Harbor runner to start."
                  : run.status === "running"
                    ? "The judge scores this task after the agent finishes."
                    : "This run has no JEV criterion details."}
              </p>
            </div>
          )}
          {screenshot && (
            <div className="screenshot">
              <div className="screenshot-heading">
                <span>Captured screenshot</span>
                <a
                  href={artifactUrl(run.id, screenshot.path)}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open image
                  <ArrowUpRight size={14} />
                </a>
              </div>
              <img
                src={artifactUrl(run.id, screenshot.path)}
                alt="Screenshot captured by the agent during this eval"
              />
            </div>
          )}
        </div>
      )}
      {tab === "trace" && (
        <div className="trace">
          {run.steps.length ? (
            run.steps.map((step) => (
              <details key={step.id}>
                <summary>
                  <span className="step-number">{step.id}</span>
                  <strong>{title(step.role)}</strong>
                  <span className="step-preview">
                    {step.text.slice(0, 110)}
                  </span>
                  <ChevronRight size={14} />
                </summary>
                <pre>{step.text}</pre>
              </details>
            ))
          ) : (
            <div className="empty compact">
              <p>The trace appears as Harbor saves the agent’s conversation.</p>
            </div>
          )}
        </div>
      )}
      {tab === "files" && (
        <div className="files">
          {run.artifacts.length ? (
            run.artifacts.map((file) => (
              <a
                key={file.path}
                href={artifactUrl(run.id, file.path)}
                target={file.image ? "_blank" : undefined}
                rel="noreferrer"
              >
                <FileText size={15} />
                <span>{file.path.replace(/^.*?\/artifacts\//, "")}</span>
                <ArrowDownToLine size={15} />
              </a>
            ))
          ) : (
            <div className="empty compact">
              <p>Files appear after Harbor collects the run’s artifacts.</p>
            </div>
          )}
        </div>
      )}
      {tab === "log" && (
        <pre className="log">
          {run.log ||
            (run.status === "queued"
              ? "Queued. Waiting for the Harbor runner to start…"
              : "Waiting for Harbor output…")}
        </pre>
      )}
    </>
  );
}

function ConfigFiles({ files }: { files: ConfigFile[] }) {
  return (
    <div className="config-files">
      {files.length ? (
        files.map((file, index) => (
          <details key={file.path} className="config-file" open={index < 2}>
            <summary>
              <FileText size={14} />
              <span>{file.path}</span>
              <ChevronRight size={14} />
            </summary>
            <pre>{file.content}</pre>
          </details>
        ))
      ) : (
        <p className="config-explanation">
          No configuration files were recorded.
        </p>
      )}
    </div>
  );
}
