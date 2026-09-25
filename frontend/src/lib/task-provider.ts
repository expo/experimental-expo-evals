import { parse, stringify, type TomlTableWithoutBigInt } from "smol-toml";
import type { ConfigFile } from "./types";

export type SandboxProvider = "e2b" | "eas-workflows";

export function sandboxProvider(files: ConfigFile[]): SandboxProvider {
  const config = files.find((file) => file.path.endsWith("/task.toml"));
  if (!config) throw new Error("task.toml is required.");
  const task = parse(config.content);
  const metadata = task.metadata;
  if (
    metadata !== undefined &&
    (!metadata ||
      typeof metadata !== "object" ||
      Array.isArray(metadata) ||
      metadata instanceof Date)
  )
    throw new Error("task.toml metadata must be a table.");
  const provider =
    (metadata as TomlTableWithoutBigInt | undefined)?.sandbox_provider ?? "e2b";
  if (provider !== "e2b" && provider !== "eas-workflows")
    throw new Error("metadata.sandbox_provider must be e2b or eas-workflows.");
  return provider;
}

export function selectSandboxProvider(
  files: ConfigFile[],
  provider: SandboxProvider,
  workflowTemplate: string,
): ConfigFile[] {
  const config = files.find((file) => file.path.endsWith("/task.toml"));
  if (!config) throw new Error("task.toml is required.");
  const task = parse(config.content);
  const metadata = task.metadata;
  if (
    metadata !== undefined &&
    (!metadata ||
      typeof metadata !== "object" ||
      Array.isArray(metadata) ||
      metadata instanceof Date)
  )
    throw new Error("task.toml metadata must be a table.");
  task.metadata = {
    ...(metadata as TomlTableWithoutBigInt | undefined),
    sandbox_provider: provider,
  };
  const next = files.map((file) =>
    file === config ? { ...file, content: stringify(task) } : file,
  );
  const path = config.path.replace(/task\.toml$/, "environment/workflow.yml");
  if (provider === "eas-workflows" && !files.some((file) => file.path === path))
    next.push({ path, content: workflowTemplate });
  return next;
}
