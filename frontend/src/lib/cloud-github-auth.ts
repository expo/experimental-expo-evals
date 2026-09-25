import { createPrivateKey } from 'node:crypto';
import { importPKCS8, SignJWT } from 'jose';
import type { CloudEnv } from './cloud-types';

type GitHubConfig = Pick<CloudEnv, 'GITHUB_REPOSITORY' | 'GITHUB_TOKEN' | 'GITHUB_APP_ID' | 'GITHUB_APP_INSTALLATION_ID' | 'GITHUB_APP_PRIVATE_KEY'>;
let cached: { key: string; token: string; expiresAt: number } | undefined;

function appSettings(env: GitHubConfig) {
  const id = env.GITHUB_APP_ID?.trim();
  const installation = env.GITHUB_APP_INSTALLATION_ID?.trim();
  const privateKey = env.GITHUB_APP_PRIVATE_KEY?.trim();
  return id && installation && /^\d+$/.test(installation) && privateKey
    ? { id, installation, privateKey }
    : undefined;
}

export function githubConfigured(env: GitHubConfig) {
  return !!(env.GITHUB_REPOSITORY && /^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPOSITORY.trim()) && (appSettings(env) || env.GITHUB_TOKEN?.trim()));
}

export async function githubToken(env: GitHubConfig): Promise<string> {
  if (!githubConfigured(env))
    throw new Error('Configure GITHUB_REPOSITORY and GitHub App credentials (or GITHUB_TOKEN) before committing.');
  const app = appSettings(env);
  if (!app) return env.GITHUB_TOKEN!.trim();

  const repository = env.GITHUB_REPOSITORY!.trim();
  // Include the key and repository so rotation or reconfiguration cannot reuse a token.
  const cacheKey = JSON.stringify([app.id, app.installation, app.privateKey, repository]);
  if (cached?.key === cacheKey && cached.expiresAt > Date.now() + 60_000)
    return cached.token;

  let jwt: string;
  try {
    // GitHub downloads PKCS#1 PEM keys; also accept PKCS#8 and dotenv's escaped newlines.
    const pem = createPrivateKey(app.privateKey.replace(/\\n/g, '\n'))
      .export({ format: 'pem', type: 'pkcs8' }).toString();
    const key = await importPKCS8(pem, 'RS256');
    const now = Math.floor(Date.now() / 1000);
    jwt = await new SignJWT({}).setProtectedHeader({ alg: 'RS256' })
      .setIssuer(app.id).setIssuedAt(now - 60).setExpirationTime(now + 540).sign(key);
  } catch {
    throw new Error('GITHUB_APP_PRIVATE_KEY must contain the unencrypted RSA PEM key downloaded from the GitHub App settings.');
  }

  let response: Response;
  try {
    response = await fetch(`https://api.github.com/app/installations/${app.installation}/access_tokens`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${jwt}`,
        Accept: 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'User-Agent': 'eas-simulator-evals',
        'X-GitHub-Api-Version': '2026-03-10',
      },
      body: JSON.stringify({ repositories: [repository.split('/')[1]], permissions: { contents: 'write' } }),
      signal: AbortSignal.timeout(25_000),
    });
  } catch {
    throw new Error('Could not reach GitHub to authenticate the app. Try again.');
  }
  if (!response.ok)
    throw new Error(`GitHub App authentication failed (${response.status}). Check the app credentials, installation, and repository access.`);
  const body = await response.json().catch(() => null) as { token?: unknown; expires_at?: unknown } | null;
  const expiresAt = typeof body?.expires_at === 'string' ? Date.parse(body.expires_at) : NaN;
  if (typeof body?.token !== 'string' || !body.token || !Number.isFinite(expiresAt) || expiresAt <= Date.now() + 60_000)
    throw new Error('GitHub returned an invalid installation token. Try again.');
  cached = { key: cacheKey, token: body.token, expiresAt: Math.min(expiresAt, Date.now() + 3_600_000) };
  return body.token;
}
