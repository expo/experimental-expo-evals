import type { CloudEnv } from "./cloud-types";
import type { ConfigFile } from "./types";
import { githubConfigured, githubToken } from "./cloud-github-auth.ts";

export const taskPaths = [
  "instruction.md",
  "environment/setup.sh",
  "tests/rubric.toml",
  "tests/test.sh",
  "task.toml",
  "environment/Dockerfile",
] as const;
export type FileConflict = {
  path: string;
  base: string;
  ours: string;
  theirs: string;
};

export function validateTaskId(id: string) {
  if (typeof id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(id))
    throw new Error(
      "Use a task ID with lowercase letters, numbers, and hyphens.",
    );
  return id;
}

export function mergeFiles(
  base: ConfigFile[],
  ours: ConfigFile[],
  theirs: ConfigFile[],
  resolutions: Record<string, string> = {},
) {
  const b = new Map(base.map((f) => [f.path, f.content]));
  const o = new Map(ours.map((f) => [f.path, f.content]));
  const t = new Map(theirs.map((f) => [f.path, f.content]));
  const paths = [...new Set([...b.keys(), ...o.keys(), ...t.keys()])].sort();
  const files: ConfigFile[] = [];
  const conflicts: FileConflict[] = [];
  for (const path of paths) {
    const bv = b.get(path),
      ov = o.get(path),
      tv = t.get(path);
    let content: string | undefined;
    if (ov === tv || tv === bv) content = ov;
    else if (ov === bv) content = tv;
    else if (Object.hasOwn(resolutions, path)) content = resolutions[path];
    else {
      conflicts.push({
        path,
        base: bv ?? "",
        ours: ov ?? "",
        theirs: tv ?? "",
      });
      continue;
    }
    if (content !== undefined) files.push({ path, content });
  }
  return { files, conflicts };
}

function githubSettings(env: CloudEnv) {
  const repository = env.GITHUB_REPOSITORY?.trim();
  const branch = env.GITHUB_BRANCH?.trim() || "main";
  if (
    !repository ||
    !/^[\w.-]+\/[\w.-]+$/.test(repository) ||
    !githubConfigured(env)
  ) {
    throw new Error(
      "Configure GITHUB_REPOSITORY, GITHUB_BRANCH, and GitHub App credentials (or GITHUB_TOKEN) before committing.",
    );
  }
  return { repository, branch };
}

class GithubError extends Error {
  staleHead: boolean;
  constructor(message: string, staleHead = false) {
    super(message);
    this.staleHead = staleHead;
  }
}
export function isStaleHead(error: unknown) {
  return error instanceof GithubError && error.staleHead;
}

async function graphql<T>(
  env: CloudEnv,
  query: string,
  variables: Record<string, unknown>,
): Promise<T> {
  const response = await fetch("https://api.github.com/graphql", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${await githubToken(env)}`,
      "Content-Type": "application/json",
      "User-Agent": "eas-simulator-evals",
    },
    body: JSON.stringify({ query, variables }),
    signal: AbortSignal.timeout(25_000),
  });
  if (!response.ok)
    throw new GithubError(
      `GitHub request failed (${response.status}). Check the token and repository permissions.`,
    );
  const body = (await response.json()) as {
    data?: T;
    errors?: { message: string; type?: string }[];
  };
  if (body.errors?.length) {
    const stale = body.errors.some((e) =>
      /expected.*(?:head|oid)|(?:head|oid).*expected|does not match|has changed|not the most recent commit/i.test(
        e.message,
      ),
    );
    throw new GithubError(
      stale
        ? "The branch changed while committing."
        : "GitHub rejected this operation. Check repository permissions and branch protection rules.",
      stale,
    );
  }
  if (!body.data) throw new GithubError("GitHub returned an empty response.");
  return body.data;
}

export async function readRemoteTask(env: CloudEnv, id: string) {
  validateTaskId(id);
  const { repository, branch } = githubSettings(env);
  const [owner, name] = repository.split("/");
  const result = await graphql<{
    repository: { ref: { target: { oid: string } } | null } | null;
  }>(
    env,
    "query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{oid}}}}",
    { owner, name, ref: `refs/heads/${branch}` },
  );
  const head = result.repository?.ref?.target.oid;
  if (!head)
    throw new Error(
      "The configured GitHub branch does not exist. Push the initial repository before committing from the UI.",
    );
  // Read every blob at the resolved commit, never at a moving branch name.
  const declarations = taskPaths.map((_, i) => `$p${i}:String!`).join(",");
  const selections = taskPaths
    .map(
      (_, i) =>
        `f${i}:object(expression:$p${i}){... on Blob{text byteSize isBinary}}`,
    )
    .join(" ");
  const variables: Record<string, unknown> = { owner, name };
  taskPaths.forEach((path, i) => {
    variables[`p${i}`] = `${head}:tasks/${id}/${path}`;
  });
  const blobs = await graphql<{
    repository: Record<
      string,
      { text: string | null; byteSize: number; isBinary: boolean } | null
    >;
  }>(
    env,
    `query($owner:String!,$name:String!,${declarations}){repository(owner:$owner,name:$name){${selections}}}`,
    variables,
  );
  const files: ConfigFile[] = [];
  taskPaths.forEach((path, i) => {
    const blob = blobs.repository[`f${i}`];
    if (!blob) return;
    if (
      blob.isBinary ||
      typeof blob.text !== "string" ||
      blob.byteSize > 100_000
    )
      throw new Error(`Remote task file is not supported: ${path}`);
    files.push({ path: `tasks/${id}/${path}`, content: blob.text });
  });
  return { head, files };
}

function base64(text: string) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192)
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

export async function createTaskCommit(
  env: CloudEnv,
  head: string,
  files: ConfigFile[],
  previous: ConfigFile[],
  message: string,
) {
  const { repository, branch } = githubSettings(env);
  const old = new Map(previous.map((f) => [f.path, f.content]));
  const paths = new Set(files.map((f) => f.path));
  const additions = files
    .filter((f) => old.get(f.path) !== f.content)
    .map((f) => ({ path: f.path, contents: base64(f.content) }));
  const deletions = previous
    .filter((f) => !paths.has(f.path))
    .map((f) => ({ path: f.path }));
  if (!additions.length && !deletions.length)
    return {
      sha: head,
      url: `https://github.com/${repository}/commit/${head}`,
    };
  const data = await graphql<{
    createCommitOnBranch: { commit: { oid: string; url: string } };
  }>(
    env,
    "mutation($input:CreateCommitOnBranchInput!){createCommitOnBranch(input:$input){commit{oid url}}}",
    {
      input: {
        branch: { repositoryNameWithOwner: repository, branchName: branch },
        expectedHeadOid: head,
        message: { headline: message },
        fileChanges: { additions, deletions },
      },
    },
  );
  return {
    sha: data.createCommitOnBranch.commit.oid,
    url: data.createCommitOnBranch.commit.url,
  };
}
