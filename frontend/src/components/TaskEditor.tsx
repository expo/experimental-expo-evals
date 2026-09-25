import { useEffect, useState } from "react";
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
import { createTaskFiles } from "../lib/new-task";
import {
  sandboxProvider,
  selectSandboxProvider,
  type SandboxProvider,
} from "../lib/task-provider";
import { easWorkflowTemplate } from "../lib/eas-workflow";

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
  const [document, setDocument] = useState(task);
  const [name, setName] = useState(task?.name ?? "");
  const [files, setFiles] = useState<ConfigFile[]>(
    () => task?.files ?? createTaskFiles(),
  );
  const [path, setPath] = useState("instruction.md");
  const field: AssistInput["field"] | null =
    path === "instruction.md"
      ? "prompt"
      : path.startsWith("environment/")
        ? "setup"
        : path === "tests/rubric.toml" || path === "tests/test.sh"
          ? "verifier"
          : null;
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
  const running = data.runs.some(
    (run) => run.status === "queued" || run.status === "running",
  );
  const agentConfigured =
    agent === "claude-code"
      ? data.settings.claudeConfigured
      : data.settings.codexConfigured;
  let provider: SandboxProvider | null = null;
  try {
    provider = sandboxProvider(files);
  } catch {
    // Raw TOML may be incomplete while editing.
  }
  const providerConfigured =
    provider === "e2b"
      ? data.settings.e2bConfigured
      : provider === "eas-workflows" && data.settings.easConfigured;
  const canRun =
    !!document &&
    !dirty &&
    !uncommitted &&
    !busy &&
    !running &&
    data.settings.runnerConfigured &&
    providerConfigured &&
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
          name: document?.name ?? id,
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
      setNotice("Draft saved.");
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
      setNotice(`Committed ${result.sha.slice(0, 7)}.`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  }
  async function assist() {
    if (!field) return;
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
    setNotice("Suggestion applied. Save to keep it.");
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
    <section className="content" aria-label="Task editor">
      <div className="row">
        <h1>{document?.id ?? "New task"}</h1>
        <span className="muted">
          {dirty || !document ? "Unsaved" : uncommitted ? "Draft" : "Committed"}
        </span>
        {document?.baseCommit && (
          <a
            href={`https://github.com/${data.settings.repository}/commit/${document.baseCommit}`}
            target="_blank"
            rel="noreferrer"
          >
            {document.baseCommit.slice(0, 7)}
          </a>
        )}
      </div>
      {newerDraft && (
        <div className="notice" role="status">
          <p>
            A newer draft exists. Copy any edits you want to keep before loading
            it.
          </p>
          <button onClick={resetToLatest} disabled={!!busy}>
            Load latest draft
          </button>
        </div>
      )}
      <fieldset disabled={!!busy}>
        {!document && (
          <label className="field">
            Task ID
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                changed();
              }}
              placeholder="my-task"
              maxLength={64}
            />
          </label>
        )}
        <label className="field">
          <span>tasks/{id || "<task-id>"}/</span>
          <select
            aria-label="Task file"
            value={path}
            onChange={(event) => {
              setPath(event.target.value);
              setProposal(null);
              setInstruction("");
              setAssistError("");
            }}
          >
            {files.map((file) => (
              <option key={suffix(file.path)} value={suffix(file.path)}>
                {suffix(file.path)}
              </option>
            ))}
          </select>
        </label>
        {path.startsWith("environment/") && (
          <label className="field">
            Sandbox
            <select
              aria-label="Sandbox provider"
              value={provider ?? ""}
              onChange={(event) => {
                try {
                  const next = event.target.value as SandboxProvider;
                  setFiles(
                    selectSandboxProvider(files, next, easWorkflowTemplate),
                  );
                  if (path !== "environment/setup.sh")
                    setPath(
                      next === "e2b"
                        ? "environment/Dockerfile"
                        : "environment/workflow.yml",
                    );
                  changed();
                  setProposal(null);
                } catch (error) {
                  setError(errorText(error));
                }
              }}
            >
              {!provider && (
                <option value="" disabled>
                  Fix task.toml to select a sandbox
                </option>
              )}
              <option value="e2b">E2B</option>
              <option value="eas-workflows">EAS Workflows (macOS)</option>
            </select>
            {provider === "eas-workflows" && (
              <span className="muted">
                Experimental. Requires EAS Workflow SSH access.
              </span>
            )}
          </label>
        )}
        <textarea
          className="file-editor"
          aria-label={path}
          spellCheck={false}
          value={read(files, path)}
          onChange={(event) => updateFile(path, event.target.value)}
        />
        {field && (
          <details className="assist">
            <summary>Suggest an edit</summary>
            <label className="field">
              Instructions
              <textarea
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder="Describe the change"
              />
            </label>
            <button
              onClick={() => void assist()}
              disabled={
                !!busy ||
                !data.settings.aiConfigured ||
                !instruction.trim() ||
                !id
              }
            >
              {busy === "assist" ? "Writing…" : "Suggest changes"}
            </button>
            {!data.settings.aiConfigured && (
              <p className="muted">Anthropic credentials required.</p>
            )}
            {assistError && (
              <p className="notice error" role="alert">
                {assistError}
              </p>
            )}
          </details>
        )}
      </fieldset>
      {proposal && (
        <section className="notice" aria-label="Suggested changes">
          <p>{proposal.explanation}</p>
          {proposal.files.map((file) => (
            <div key={file.path}>
              <p>{suffix(file.path)}</p>
              <div className="diff">
                <div>
                  <small>Current</small>
                  <pre>{read(files, suffix(file.path))}</pre>
                </div>
                <div>
                  <small>Suggested</small>
                  <pre>{file.content}</pre>
                </div>
              </div>
            </div>
          ))}
          <div className="row">
            <button disabled={!!busy} onClick={() => setProposal(null)}>
              Discard
            </button>
            <button disabled={!!busy} onClick={applyProposal}>
              Apply to draft
            </button>
          </div>
        </section>
      )}
      <div className="row actions">
        <button
          onClick={() => void save()}
          disabled={!!busy || !valid || (!dirty && !!document)}
        >
          {busy === "save" ? "Saving…" : "Save draft"}
        </button>
        {dirty && <span className="muted">Unsaved changes</span>}
      </div>
      {document && uncommitted && (
        <section className="panel" aria-label="Commit changes">
          <div className="row">
            <label className="field grow">
              Commit message
              <input
                value={commitMessage}
                onChange={(event) => setCommitMessage(event.target.value)}
                disabled={!!busy}
                maxLength={160}
                placeholder="describe the change"
              />
            </label>
            <button
              onClick={() => void commit()}
              disabled={
                !!busy ||
                dirty ||
                !data.settings.gitConfigured ||
                !commitMessage.trim() ||
                !!conflict
              }
            >
              {busy === "commit" ? "Committing…" : "Commit changes"}
            </button>
          </div>
          {dirty && <p className="muted">Save before committing.</p>}
          {!data.settings.gitConfigured && (
            <p className="muted">GitHub connection required.</p>
          )}
          {conflict && (
            <section className="notice" aria-label="Resolve merge conflicts">
              <p>{conflict.message}</p>
              {conflict.conflicts.map((item) => (
                <div key={item.path}>
                  <p>{item.path}</p>
                  <div className="diff">
                    <div>
                      <small>Saved draft</small>
                      <pre>{item.ours}</pre>
                      <button
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
                      <small>Repository</small>
                      <pre>{item.theirs}</pre>
                      <button
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
                  <details>
                    <summary>Common base</summary>
                    <pre>{item.base}</pre>
                  </details>
                  <label className="field">
                    Merged content
                    <textarea
                      spellCheck={false}
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
              <div className="row">
                <button
                  disabled={!!busy}
                  onClick={() => {
                    setConflict(null);
                    setResolutions({});
                  }}
                >
                  Cancel
                </button>
                <button
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
                  {busy === "commit" ? "Committing…" : "Commit resolved files"}
                </button>
              </div>
            </section>
          )}
        </section>
      )}
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="muted" role="status">
          {notice}
        </p>
      )}
      {document && (
        <section className="panel" aria-label="Run task">
          <div className="row">
            <label className="field">
              Agent
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
            <label className="field grow">
              Model
              <input
                value={model}
                disabled={!!busy}
                onChange={(event) => setModel(event.target.value)}
                placeholder="provider/model"
                spellCheck={false}
              />
            </label>
            <button disabled={!canRun} onClick={() => void run()}>
              {busy === "run" ? "Starting…" : "Run"}
            </button>
          </div>
          <p className="muted">
            {data.settings.local
              ? "Run from the deployed app or Harbor CLI."
              : dirty || uncommitted
                ? "Save and commit before running."
                : running
                  ? "Another run is in progress."
                  : !data.settings.runnerConfigured
                    ? "Runner credentials required."
                    : !provider
                      ? "Fix the sandbox provider in task.toml."
                      : !providerConfigured
                        ? provider === "eas-workflows"
                          ? "EAS project configuration required."
                          : "E2B credentials required."
                        : !agentConfigured
                          ? `${agent === "codex" ? "OpenAI" : "Anthropic"} credentials required.`
                          : `${provider === "eas-workflows" ? "EAS Workflows (macOS)" : "E2B"} · 1 attempt · commit ${document.baseCommit?.slice(0, 7)}`}
          </p>
        </section>
      )}
    </section>
  );
}
