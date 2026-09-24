import { useEffect, useState } from "react";
import {
  Check,
  ChevronRight,
  FileText,
  GitCommitHorizontal,
  LoaderCircle,
  Play,
  Save,
  Sparkles,
  X,
} from "lucide-react";
import {
  assistDraft,
  commitTask,
  saveDraft,
  startRun,
} from "../lib/cloud.functions";
import type {
  AssistInput,
  AssistResult,
  CloudDashboard,
  CommitResult,
  TaskDocument,
} from "../lib/cloud-types";
import type { Agent, ConfigFile } from "../lib/types";

type Conflict = Extract<CommitResult, { ok: false }>;
const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
const slug = (value: string) =>
  value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
const suffix = (path: string) => path.replace(/^tasks\/[^/]+\//, "");
const read = (files: ConfigFile[], path: string) =>
  files.find((file) => suffix(file.path) === path)?.content ?? "";
const names = {
  prompt: "Task prompt",
  setup: "Setup script",
  verifier: "Verifier",
};
const templates = {
  prompt: "Make the task more specific, with a clear outcome.",
  setup: "Install the tools needed for this task.",
  verifier:
    "Write clear JEV criteria that can be assessed from the agent trace.",
};

export function TaskEditor({
  task,
  data,
  onDirty,
  onBusy,
  onSaved,
  onStarted,
}: {
  task?: TaskDocument;
  data: CloudDashboard;
  onDirty: (value: boolean) => void;
  onBusy: (value: boolean) => void;
  onSaved: (task: TaskDocument) => Promise<void>;
  onStarted: (id: string) => Promise<void>;
}) {
  const template =
    data.tasks.find((item) => item.id === "coffee-timer") ?? data.tasks[0];
  const [document, setDocument] = useState(task);
  const [name, setName] = useState(task?.name ?? "");
  const [files, setFiles] = useState<ConfigFile[]>(
    () =>
      task?.files ??
      template?.files.map((file) => ({
        ...file,
        content: suffix(file.path) === "instruction.md" ? "" : file.content,
      })) ??
      [],
  );
  const [section, setSection] = useState<"edit" | "config">("edit");
  const [field, setField] = useState<AssistInput["field"]>("prompt");
  const [instruction, setInstruction] = useState("");
  const [proposal, setProposal] = useState<AssistResult | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"save" | "commit" | "assist" | "run" | null>(
    null,
  );
  const [error, setError] = useState("");
  const [assistError, setAssistError] = useState("");
  const [notice, setNotice] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [agent, setAgent] = useState<Agent>("claude-code");
  const [model, setModel] = useState("anthropic/claude-opus-5-5");
  const id = document?.id ?? slug(name);
  const valid = !!id && !!name.trim() && !!read(files, "instruction.md").trim();
  const uncommitted =
    !document?.baseCommit || document.committedRevision !== document.revision;
  const newerDraft = !!task && !!document && task.revision > document.revision;
  const running = data.runs.some((run) => run.status === "running");
  const agentConfigured =
    agent === "claude-code"
      ? data.settings.claudeConfigured
      : data.settings.codexConfigured;
  const canRun =
    !!document &&
    !dirty &&
    !uncommitted &&
    !busy &&
    !running &&
    data.settings.runnerConfigured &&
    agentConfigured &&
    !!model.trim();

  useEffect(() => {
    onBusy(!!busy);
    return () => onBusy(false);
  }, [busy, onBusy]);

  useEffect(() => {
    if (
      task &&
      (!document || task.revision >= document.revision) &&
      !dirty &&
      !busy &&
      !conflict
    ) {
      setDocument(task);
      setFiles(task.files);
      setName(task.name);
    }
  }, [
    task?.revision,
    task?.baseCommit,
    task?.committedRevision,
    dirty,
    busy,
    conflict,
  ]);

  function changed() {
    setDirty(true);
    onDirty(true);
    setNotice("");
    setError("");
    setConflict(null);
    setResolutions({});
  }
  function updateFile(path: string, content: string) {
    setFiles((current) => {
      const exists = current.some((file) => suffix(file.path) === path);
      return exists
        ? current.map((file) =>
            suffix(file.path) === path ? { ...file, content } : file,
          )
        : [...current, { path: `tasks/${id}/${path}`, content }];
    });
    changed();
  }
  function downloadDraft() {
    const blob = new Blob(
      [JSON.stringify({ name, files: preparedFiles() }, null, 2)],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const link = window.document.createElement("a");
    link.href = url;
    link.download = `${id || "task"}-draft.json`;
    link.click();
    URL.revokeObjectURL(url);
  }
  function resetToLatest() {
    if (
      !task ||
      (dirty &&
        !window.confirm(
          "Discard your browser edits and load the latest saved draft?",
        ))
    )
      return;
    setDocument(task);
    setFiles(task.files);
    setName(task.name);
    setDirty(false);
    onDirty(false);
    setProposal(null);
    setConflict(null);
    setError("");
    setNotice("Loaded the latest saved draft.");
  }
  const preparedFiles = () =>
    files.map((file) => ({
      ...file,
      path: `tasks/${id}/${suffix(file.path)}`,
    }));
  async function save() {
    setBusy("save");
    setError("");
    setNotice("");
    try {
      const saved = await saveDraft({
        data: {
          taskId: id,
          name: name.trim(),
          revision: document?.revision ?? null,
          files: preparedFiles(),
        },
      });
      setDocument(saved);
      setFiles(saved.files);
      setDirty(false);
      onDirty(false);
      setConflict(null);
      setResolutions({});
      await onSaved(saved);
      setNotice(
        `Draft revision ${saved.revision} saved to D1. Commit when it is ready to run.`,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }
  async function commit() {
    if (!document) return;
    setBusy("commit");
    setError("");
    setNotice("");
    try {
      const result = await commitTask({
        data: {
          taskId: document.id,
          revision: document.revision,
          message: commitMessage.trim(),
          ...(conflict ? { expectedHead: conflict.head, resolutions } : {}),
        },
      });
      if (!result.ok) {
        setConflict(result);
        setResolutions({});
        return;
      }
      setDocument(result.task);
      setFiles(result.task.files);
      setName(result.task.name);
      setConflict(null);
      setResolutions({});
      setCommitMessage("");
      setProposal(null);
      await onSaved(result.task);
      setNotice(
        `Committed ${result.sha.slice(0, 7)}. This version is ready to run.`,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }
  async function assist() {
    setBusy("assist");
    setError("");
    setAssistError("");
    setNotice("");
    setProposal(null);
    try {
      setProposal(
        await assistDraft({
          data: {
            taskId: id,
            files: preparedFiles(),
            field,
            instruction: instruction.trim(),
          },
        }),
      );
    } catch (err) {
      setAssistError(errorText(err));
    } finally {
      setBusy(null);
    }
  }
  function applyProposal() {
    if (!proposal) return;
    const replacements = new Map(
      proposal.files.map((file) => [suffix(file.path), file]),
    );
    setFiles((current) => [
      ...current.map((file) =>
        replacements.has(suffix(file.path))
          ? { ...file, content: replacements.get(suffix(file.path))!.content }
          : file,
      ),
      ...proposal.files.filter(
        (file) =>
          !current.some((old) => suffix(old.path) === suffix(file.path)),
      ),
    ]);
    changed();
    setProposal(null);
    setNotice(
      "Suggestion applied to your browser draft. Review it, then save.",
    );
  }
  async function run() {
    if (!document || !canRun) return;
    setBusy("run");
    setError("");
    try {
      const result = await startRun({
        data: {
          taskId: document.id,
          revision: document.revision,
          agent,
          model: model.trim(),
        },
      });
      await onStarted(result.id);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="editor">
      <div className="section-heading">
        <div>
          <p className="eyebrow">TASK DEFINITION</p>
          <h2>{document?.name ?? "Create a task"}</h2>
        </div>
        <span
          className={`badge ${!dirty && !uncommitted ? "passed" : "neutral"}`}
        >
          {dirty || !document
            ? "Unsaved edits"
            : uncommitted
              ? "Saved draft"
              : "Committed"}
        </span>
      </div>
      <p className="save-explanation">
        Save draft persists to D1. Commit changes creates a Git version. Runs
        use the committed version.
      </p>
      {document && (
        <div className="document-meta">
          <span>
            Revision {document.revision} · {document.updatedBy}
          </span>
          {document.baseCommit && (
            <a
              className="commit-sha"
              href={`https://github.com/${data.settings.repository}/commit/${document.baseCommit}`}
              target="_blank"
              rel="noreferrer"
              title="Open Git commit"
            >
              {document.baseCommit.slice(0, 7)}
            </a>
          )}
        </div>
      )}
      {newerDraft && (
        <div className="notice stale-notice" role="status">
          <span>
            A newer draft was saved by someone else. Your browser edits are
            intact. Saving will check for this conflict.
          </span>
          <button className="button secondary" onClick={downloadDraft}>
            Download my edits
          </button>
          <button
            className="button secondary"
            onClick={resetToLatest}
            disabled={!!busy}
          >
            Load latest draft
          </button>
        </div>
      )}
      <nav className="result-tabs task-tabs" aria-label="Task sections">
        <button
          className={section === "edit" ? "active" : ""}
          onClick={() => setSection("edit")}
          aria-pressed={section === "edit"}
        >
          Editor
        </button>
        <button
          className={section === "config" ? "active" : ""}
          onClick={() => setSection("config")}
          aria-pressed={section === "config"}
        >
          Saved configuration
        </button>
      </nav>
      {section === "edit" ? (
        <>
          <fieldset className="editor-fields" disabled={!!busy}>
            {!document && (
              <label className="field">
                <span>Task name</span>
                <input
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                    changed();
                  }}
                  placeholder="e.g. Weather app"
                  maxLength={80}
                />
                <small>
                  {id
                    ? `tasks/${id}/`
                    : "Creates a new task using the coffee timer environment."}
                </small>
              </label>
            )}
            <nav className="field-tabs" aria-label="Task files">
              {(["prompt", "setup", "verifier"] as const).map((item) => (
                <button
                  type="button"
                  key={item}
                  className={field === item ? "active" : ""}
                  aria-pressed={field === item}
                  onClick={() => {
                    setField(item);
                    setProposal(null);
                    setInstruction("");
                  }}
                >
                  {names[item]}
                </button>
              ))}
            </nav>
            {field === "prompt" && (
              <label className="field">
                <span>
                  Task prompt <code>instruction.md</code>
                </span>
                <small>The instruction Harbor gives to the agent.</small>
                <textarea
                  className="prompt-input main-file-input"
                  value={read(files, "instruction.md")}
                  onChange={(event) =>
                    updateFile("instruction.md", event.target.value)
                  }
                  placeholder="Create a new Expo app…"
                />
              </label>
            )}
            {field === "setup" && (
              <label className="field">
                <span>
                  Setup script <code>environment/setup.sh</code>
                </span>
                <small>
                  Installs task dependencies in the E2B environment. Shell
                  commands run during the environment build.
                </small>
                <textarea
                  className="code-input main-file-input"
                  spellCheck={false}
                  value={read(files, "environment/setup.sh")}
                  onChange={(event) =>
                    updateFile("environment/setup.sh", event.target.value)
                  }
                />
              </label>
            )}
            {field === "verifier" && (
              <>
                <label className="field">
                  <span>
                    Judge rubric <code>tests/rubric.toml</code>
                  </span>
                  <small>
                    Native RewardKit configuration: judge, criteria, and score
                    aggregation.
                  </small>
                  <textarea
                    className="code-input rubric-input"
                    spellCheck={false}
                    value={read(files, "tests/rubric.toml")}
                    onChange={(event) =>
                      updateFile("tests/rubric.toml", event.target.value)
                    }
                  />
                </label>
                <details className="verifier-script">
                  <summary>
                    <ChevronRight size={14} /> Verifier entrypoint ·
                    tests/test.sh
                  </summary>
                  <label className="field">
                    <span className="sr-only">Verifier entrypoint</span>
                    <textarea
                      className="code-input main-file-input"
                      spellCheck={false}
                      value={read(files, "tests/test.sh")}
                      onChange={(event) =>
                        updateFile("tests/test.sh", event.target.value)
                      }
                    />
                  </label>
                </details>
              </>
            )}
            <div className="assist-panel">
              <label className="field">
                <span>
                  <Sparkles size={14} /> Help write {names[field].toLowerCase()}
                </span>
                <small>
                  The assistant suggests changes for review. Nothing is saved or
                  committed automatically.
                </small>
                <textarea
                  className="assist-input"
                  value={instruction}
                  onChange={(event) => setInstruction(event.target.value)}
                  placeholder={templates[field]}
                  aria-label={`Instructions for ${names[field].toLowerCase()} assistant`}
                />
              </label>
              <div className="assist-footer">
                <span>
                  {data.settings.aiConfigured
                    ? "Uses the workspace’s Anthropic API key."
                    : "Add an Anthropic API key to enable suggestions."}
                </span>
                <button
                  className="button secondary"
                  type="button"
                  onClick={() => void assist()}
                  disabled={
                    !!busy ||
                    !data.settings.aiConfigured ||
                    !instruction.trim() ||
                    !id
                  }
                >
                  {busy === "assist" ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Sparkles size={14} />
                  )}{" "}
                  {busy === "assist"
                    ? "Writing suggestion…"
                    : "Suggest changes"}
                </button>
              </div>
              {assistError && (
                <div className="notice error" role="alert">
                  {assistError}
                </div>
              )}
            </div>
          </fieldset>
          {proposal && (
            <section className="proposal-panel" aria-label="Suggested changes">
              <div className="review-heading">
                <strong>Review suggestion</strong>
                <span className="badge neutral">Not applied</span>
              </div>
              <p className="config-explanation">{proposal.explanation}</p>
              {proposal.files.map((file) => (
                <div className="proposal-file" key={file.path}>
                  <p className="file-path">{suffix(file.path)}</p>
                  <div className="diff-columns">
                    <div>
                      <span>Current draft</span>
                      <pre>
                        {read(files, suffix(file.path)) || "Empty file"}
                      </pre>
                    </div>
                    <div>
                      <span>Suggested</span>
                      <pre>{file.content || "Empty file"}</pre>
                    </div>
                  </div>
                </div>
              ))}
              <div className="review-actions">
                <button
                  className="button secondary"
                  disabled={!!busy}
                  onClick={() => setProposal(null)}
                >
                  <X size={14} />
                  Discard
                </button>
                <button
                  className="button primary"
                  disabled={!!busy}
                  onClick={applyProposal}
                >
                  <Check size={14} />
                  Apply to draft
                </button>
              </div>
            </section>
          )}
          <div className="save-row">
            <span>
              {dirty
                ? "Changes are only in this browser until saved."
                : document
                  ? "All draft changes are saved to D1."
                  : "Start with a name and task prompt."}
            </span>
            <button
              className="button secondary"
              onClick={() => void save()}
              disabled={!!busy || !valid || (!dirty && !!document)}
            >
              {busy === "save" ? (
                <LoaderCircle size={15} className="spin" />
              ) : (
                <Save size={15} />
              )}
              Save draft
            </button>
          </div>
        </>
      ) : (
        <div className="task-config">
          <p className="config-explanation">
            Exact native files in the saved D1 draft.{" "}
            {dirty ? "Your browser has unsaved edits." : ""}
          </p>
          {document?.files.map((file, index) => (
            <details className="config-file" key={file.path} open={index < 2}>
              <summary>
                <FileText size={14} />
                <span>{file.path}</span>
                <ChevronRight size={14} />
              </summary>
              <pre>{file.content}</pre>
            </details>
          ))}
          {!document && (
            <p className="config-explanation">
              Save this task to see its persisted configuration.
            </p>
          )}
        </div>
      )}
      <div className="commit-panel">
        <div className="launch-title">
          <span className="eyebrow">COMMIT CHANGES</span>
          <span className="subtle">{data.settings.branch}</span>
        </div>
        <p className="config-explanation">
          {!data.settings.gitConfigured
            ? "Connect a GitHub repository and token to enable commits. Saved drafts still persist in D1."
            : dirty
              ? "Save your draft before committing."
              : !uncommitted
                ? "This saved revision is already committed."
                : "Creates one commit containing this task’s saved files. Other task changes are kept."}
        </p>
        <div className="commit-controls">
          <label className="field">
            <span className="sr-only">Commit message</span>
            <input
              placeholder="describe the change"
              aria-label="Commit message"
              value={commitMessage}
              onChange={(event) => setCommitMessage(event.target.value)}
              disabled={!!busy}
              maxLength={160}
            />
          </label>
          <button
            className="button secondary"
            onClick={() => void commit()}
            disabled={
              !!busy ||
              dirty ||
              !document ||
              !uncommitted ||
              !data.settings.gitConfigured ||
              !commitMessage.trim() ||
              !!conflict
            }
          >
            {busy === "commit" ? (
              <LoaderCircle size={15} className="spin" />
            ) : (
              <GitCommitHorizontal size={15} />
            )}
            Commit changes
          </button>
        </div>
        <p className="launch-note">
          Concurrent edits to different files merge automatically. If the same
          file changed, review both versions below before committing.
        </p>
        {conflict && (
          <section
            className="conflict-panel"
            aria-label="Resolve merge conflicts"
          >
            <div className="review-heading">
              <strong>
                Resolve {conflict.conflicts.length} conflicting{" "}
                {conflict.conflicts.length === 1 ? "file" : "files"}
              </strong>
              <span className="commit-sha">{conflict.head.slice(0, 7)}</span>
            </div>
            <p className="config-explanation">
              {conflict.message} Choose a version or edit the merged content.
              The branch is checked again when you commit.
            </p>
            {conflict.conflicts.map((item) => (
              <div className="conflict-file" key={item.path}>
                <p className="file-path">{item.path}</p>
                <div className="diff-columns">
                  <div>
                    <span>Your saved draft</span>
                    <pre>{item.ours || "Empty file"}</pre>
                    <button
                      className="button secondary"
                      disabled={!!busy}
                      onClick={() =>
                        setResolutions((current) => ({
                          ...current,
                          [item.path]: item.ours,
                        }))
                      }
                    >
                      Use draft
                    </button>
                  </div>
                  <div>
                    <span>Current repository</span>
                    <pre>{item.theirs || "Empty file"}</pre>
                    <button
                      className="button secondary"
                      disabled={!!busy}
                      onClick={() =>
                        setResolutions((current) => ({
                          ...current,
                          [item.path]: item.theirs,
                        }))
                      }
                    >
                      Use repository
                    </button>
                  </div>
                </div>
                <details className="conflict-base">
                  <summary>Common base</summary>
                  <pre>{item.base || "File did not exist"}</pre>
                </details>
                <label className="field">
                  <span>
                    Merged content{" "}
                    {Object.hasOwn(resolutions, item.path) ? (
                      <Check size={14} />
                    ) : (
                      <span className="label-tag">Choose or edit</span>
                    )}
                  </span>
                  <textarea
                    className="code-input"
                    aria-label={`Merged content for ${item.path}`}
                    value={resolutions[item.path] ?? item.ours}
                    disabled={!!busy}
                    onChange={(event) =>
                      setResolutions((current) => ({
                        ...current,
                        [item.path]: event.target.value,
                      }))
                    }
                  />
                </label>
              </div>
            ))}
            <div className="review-actions">
              <button
                className="button secondary"
                disabled={!!busy}
                onClick={() => {
                  setConflict(null);
                  setResolutions({});
                }}
              >
                Cancel resolution
              </button>
              <button
                className="button primary"
                onClick={() => void commit()}
                disabled={
                  !!busy ||
                  dirty ||
                  !commitMessage.trim() ||
                  !conflict.conflicts.every((item) =>
                    Object.hasOwn(resolutions, item.path),
                  )
                }
              >
                {busy === "commit" ? (
                  <LoaderCircle size={14} className="spin" />
                ) : (
                  <GitCommitHorizontal size={14} />
                )}
                Commit resolved files
              </button>
            </div>
          </section>
        )}
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice success" role="status">
          <Check size={16} />
          {notice}
        </div>
      )}
      <div className="launch-panel">
        <div className="launch-title">
          <span className="eyebrow">RUN COMMITTED TASK</span>
          <span className="subtle">1 attempt · E2B</span>
        </div>
        <div className="run-controls">
          <label className="field">
            <span>Agent</span>
            <select
              value={agent}
              disabled={!!busy}
              onChange={(event) => {
                const next = event.target.value as Agent;
                setAgent(next);
                setModel(
                  next === "claude-code"
                    ? "anthropic/claude-opus-5-5"
                    : "openai/gpt-5.4",
                );
              }}
            >
              <option value="claude-code">Claude Code</option>
              <option value="codex">Codex</option>
            </select>
          </label>
          <label className="field model-field">
            <span>Model</span>
            <input
              value={model}
              disabled={!!busy}
              onChange={(event) => setModel(event.target.value)}
              placeholder="provider/model"
              spellCheck={false}
            />
          </label>
          <button
            className="button primary"
            disabled={!canRun}
            onClick={() => void run()}
          >
            {busy === "run" ? (
              <LoaderCircle size={15} className="spin" />
            ) : (
              <Play size={15} fill="currentColor" />
            )}
            {busy === "run" ? "Starting…" : "Run eval"}
          </button>
        </div>
        <p className="launch-note">
          {dirty || uncommitted
            ? "Save and commit all changes before running this task."
            : running
              ? "Another eval is running. Follow its progress in Runs."
              : !data.settings.runnerConfigured
                ? "Configure the Harbor runner and E2B, Expo, and TypeSafe credentials to run evals."
                : !agentConfigured
                  ? `Add ${agent === "codex" ? "OpenAI" : "Anthropic"} credentials to run this agent.`
                  : `Runs task revision ${document?.revision} from commit ${document?.baseCommit?.slice(0, 7)}. Model and service usage apply.`}
        </p>
        <p className="launch-note">
          Agent and model apply to this run only. Starting a run does not change
          or commit task files.
        </p>
      </div>
    </section>
  );
}
