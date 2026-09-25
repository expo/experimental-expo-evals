import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "smol-toml";
import { createTaskFiles } from "./new-task.ts";
import { sandboxProvider, selectSandboxProvider } from "./task-provider.ts";
import { validateFiles } from "./cloud-store.ts";
import { easWorkflowTemplate } from "./eas-workflow.ts";

const workflow = readFileSync(
  new URL("../../../eas_provider/workflow.yml", import.meta.url),
  "utf8",
);
const files = createTaskFiles("example").map((file) =>
  file.path.endsWith("/instruction.md")
    ? { ...file, content: "Create a timer." }
    : file,
);

test("legacy six-file tasks default to E2B and switching retains both native definitions", () => {
  assert.equal(easWorkflowTemplate, workflow);
  assert.equal(sandboxProvider(files), "e2b");
  assert.equal(validateFiles("example", files).length, 6);
  const eas = selectSandboxProvider(
    files,
    "eas-workflows",
    easWorkflowTemplate,
  );
  assert.equal(sandboxProvider(eas), "eas-workflows");
  assert.equal(validateFiles("example", eas).length, 7);
  assert.equal(
    eas.find((file) => file.path.endsWith("/workflow.yml"))?.content,
    workflow,
  );
  const original = parse(
    files.find((file) => file.path.endsWith("/task.toml"))!.content,
  );
  const selected = parse(
    eas.find((file) => file.path.endsWith("/task.toml"))!.content,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(selected)),
    JSON.parse(
      JSON.stringify({
        ...original,
        metadata: { sandbox_provider: "eas-workflows" },
      }),
    ),
  );
  const back = selectSandboxProvider(
    eas,
    "e2b",
    "must not replace saved workflow",
  );
  assert.equal(sandboxProvider(back), "e2b");
  assert.deepEqual(
    back.filter((file) => file.path.includes("/environment/")),
    eas.filter((file) => file.path.includes("/environment/")),
  );
  assert.equal(validateFiles("example", back).length, 7);
});

test("each sandbox requires its own definition and unknown providers are rejected", () => {
  const eas = selectSandboxProvider(files, "eas-workflows", workflow);
  assert.throws(
    () =>
      validateFiles(
        "example",
        eas.filter((file) => !file.path.endsWith("/workflow.yml")),
      ),
    /workflow.yml is required/,
  );
  assert.throws(
    () =>
      validateFiles(
        "example",
        eas.map((file) =>
          file.path.endsWith("/workflow.yml")
            ? { ...file, content: " " }
            : file,
        ),
      ),
    /workflow.yml is required/,
  );
  const noDocker = eas.map((file) =>
    file.path.endsWith("/Dockerfile") ? { ...file, content: "" } : file,
  );
  assert.equal(validateFiles("example", noDocker).length, 7);
  assert.throws(
    () =>
      validateFiles(
        "example",
        selectSandboxProvider(noDocker, "e2b", workflow),
      ),
    /Dockerfile is required/,
  );
  assert.throws(
    () =>
      validateFiles(
        "example",
        eas.map((file) =>
          file.path.endsWith("/task.toml")
            ? {
                ...file,
                content: file.content.replace("eas-workflows", "unknown"),
              }
            : file,
        ),
      ),
    /sandbox_provider/,
  );
  assert.throws(
    () =>
      validateFiles(
        "example",
        eas.map((file) =>
          file.path.endsWith("/workflow.yml")
            ? { ...file, path: "tasks/example/environment/../../secret.yml" }
            : file,
        ),
      ),
    /file path/,
  );
});
