import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { dashboard, runDetail } from "../lib/cloud.functions";
import type { ConfigFile, RunDetail } from "../lib/types";
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
  return (
    <main className="workspace">
      <nav className="tabs" aria-label="Workspace sections">
        {(["tasks", "runs"] as const).map((item) => (
          <button
            key={item}
            aria-pressed={view === item}
            disabled={mutationPending}
            onClick={() => switchView(item)}
          >
            {title(item)} <span className="muted">{data[item].length}</span>
          </button>
        ))}
      </nav>
      {refreshError && (
        <p className="notice" role="status">
          {refreshError}
        </p>
      )}
      {view === "tasks" ? (
        <div className="layout">
          <aside className="sidebar" aria-label="Tasks">
            <button
              className="new-task"
              disabled={mutationPending}
              onClick={() => selectTask(null)}
            >
              New task
            </button>
            {data.tasks.map((item) => (
              <button
                key={item.id}
                className="list-item"
                aria-current={item.id === taskId ? "true" : undefined}
                disabled={mutationPending}
                onClick={() => selectTask(item.id)}
              >
                <span>{item.id}</span>
                <small>
                  {item.committedRevision === item.revision && item.baseCommit
                    ? "Committed"
                    : "Draft"}
                </small>
              </button>
            ))}
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
        <div className="layout">
          <aside className="sidebar" aria-label="Runs">
            {data.runs.length === 0 && <p className="muted">No runs yet.</p>}
            {data.runs.map((run) => (
              <button
                key={run.id}
                className="list-item"
                aria-current={run.id === runId ? "true" : undefined}
                onClick={() => selectRun(run.id)}
              >
                <span>{run.task}</span>
                <small>
                  {title(run.status)} · {date(run.startedAt)}
                </small>
                <small>{run.model || run.agent}</small>
              </button>
            ))}
          </aside>
          <section className="content" aria-label="Run details">
            {detailError ? (
              <p className="notice error" role="alert">
                {detailError}
              </p>
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
              <p className="muted">
                {runId ? "Loading run…" : "Select a run."}
              </p>
            )}
          </section>
        </div>
      )}
    </main>
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
  useEffect(() => setTab("scores"), [run.id]);
  return (
    <>
      <div className="row">
        <h1>{run.task}</h1>
        <span className="muted">{title(run.status)}</span>
      </div>
      <p className="muted">
        {run.agent} · {run.model}
      </p>
      <p className="muted">
        {date(run.startedAt)}
        {commitSha && (
          <>
            {" "}
            ·{" "}
            <a
              href={`https://github.com/${repository}/commit/${commitSha}`}
              target="_blank"
              rel="noreferrer"
            >
              {commitSha.slice(0, 7)}
            </a>
          </>
        )}
      </p>
      <dl className="metrics">
        <div>
          <dt>Reward</dt>
          <dd>{run.score ?? "—"}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{duration(run.durationSec)}</dd>
        </div>
        <div>
          <dt>Agent cost</dt>
          <dd>{money(run.costUsd)}</dd>
        </div>
      </dl>
      {run.error && (
        <p className="notice error" role="alert">
          {run.error}
        </p>
      )}
      <nav className="tabs" aria-label="Run sections">
        {(["scores", "trace", "config", "files", "log"] as const).map(
          (item) => (
            <button
              key={item}
              aria-pressed={tab === item}
              onClick={() => setTab(item)}
            >
              {item === "config" ? "Configuration" : title(item)}
            </button>
          ),
        )}
      </nav>
      {tab === "scores" && (
        <>
          {run.criteria.length ? (
            <>
              <table>
                <thead>
                  <tr>
                    <th>Criterion</th>
                    <th>Score</th>
                    <th>JEV probability</th>
                  </tr>
                </thead>
                <tbody>
                  {run.criteria.map((criterion) => (
                    <tr key={criterion.name}>
                      <td>
                        {criterion.name}
                        <p className="muted">{criterion.description}</p>
                      </td>
                      <td>{criterion.score}</td>
                      <td>{criterion.probability ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                JEV grades the recorded trace, not the app’s visual or
                interactive correctness.
              </p>
            </>
          ) : (
            <p className="muted">No criterion scores yet.</p>
          )}
        </>
      )}
      {tab === "trace" &&
        (run.steps.length ? (
          run.steps.map((step) => (
            <details key={step.id}>
              <summary>
                {step.id} · {step.role}
              </summary>
              <pre>{step.text}</pre>
            </details>
          ))
        ) : (
          <p className="muted">No trace yet.</p>
        ))}
      {tab === "config" && <ConfigFiles files={run.configFiles} />}
      {tab === "files" &&
        (run.artifacts.length ? (
          run.artifacts.map((file) => (
            <div className="artifact" key={file.path}>
              <a
                href={artifactUrl(run.id, file.path)}
                target="_blank"
                rel="noreferrer"
              >
                {file.path}
              </a>
              {file.image && (
                <details>
                  <summary>Preview image</summary>
                  <img
                    src={artifactUrl(run.id, file.path)}
                    alt={file.name}
                    loading="lazy"
                  />
                </details>
              )}
            </div>
          ))
        ) : (
          <p className="muted">No files yet.</p>
        ))}
      {tab === "log" && <pre>{run.log || "No output yet."}</pre>}
    </>
  );
}

function ConfigFiles({ files }: { files: ConfigFile[] }) {
  return files.length ? (
    files.map((file) => (
      <details key={file.path}>
        <summary>{file.path}</summary>
        <pre>{file.content}</pre>
      </details>
    ))
  ) : (
    <p className="muted">No configuration recorded.</p>
  );
}
