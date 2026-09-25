import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { test } from 'node:test';
import { importSPKI, jwtVerify } from 'jose';
import { githubConfigured, githubToken } from './cloud-github-auth.ts';
import { createTaskCommit } from './cloud-github.ts';
import type { CloudEnv } from './cloud-types';

// Generated per process; no reusable credential is stored in this fixture.
const keys = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pkcs1 = keys.privateKey.export({ format: 'pem', type: 'pkcs1' }).toString();
const pkcs8 = keys.privateKey.export({ format: 'pem', type: 'pkcs8' }).toString();
const publicKey = await importSPKI(keys.publicKey.export({ format: 'pem', type: 'spki' }).toString(), 'RS256');
const appEnv = (id: string) => ({
  GITHUB_REPOSITORY: 'company/evals',
  GITHUB_APP_ID: id,
  GITHUB_APP_INSTALLATION_ID: '12345',
  GITHUB_APP_PRIVATE_KEY: pkcs1,
} as CloudEnv);

test('PAT fallback needs no token exchange and incomplete credentials fail clearly', async (t) => {
  t.mock.method(globalThis, 'fetch', () => { throw new Error('Unexpected network request'); });
  const env = { GITHUB_REPOSITORY: 'company/evals', GITHUB_TOKEN: 'test-pat' };
  assert.equal(githubConfigured(env), true);
  assert.equal(await githubToken(env), 'test-pat');
  assert.equal(githubConfigured({ ...env, GITHUB_REPOSITORY: '../bad/repo' }), false);
  assert.equal(githubConfigured({ GITHUB_REPOSITORY: 'company/evals', GITHUB_APP_ID: '123' }), false);
  await assert.rejects(githubToken({ GITHUB_REPOSITORY: 'company/evals' }), /GitHub App credentials/);
});

test('app signs a valid JWT, scopes the token, commits as the app, and refreshes before expiry', async (t) => {
  let now = Date.now();
  t.mock.method(Date, 'now', () => now);
  const env = { ...appEnv('Iv1.test-client-id'), GITHUB_TOKEN: 'unused-personal-token' };
  let exchanges = 0;
  t.mock.method(globalThis, 'fetch', async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    if (url === 'https://api.github.com/graphql') {
      assert.equal(headers.get('Authorization'), 'Bearer installation-1');
      const { query, variables } = JSON.parse(String(init.body));
      assert.match(query, /createCommitOnBranch/);
      assert.equal(variables.input.expectedHeadOid, 'a'.repeat(40));
      return Response.json({ data: { createCommitOnBranch: { commit: { oid: 'b'.repeat(40), url: 'https://github.com/company/evals/commit/b' } } } });
    }
    assert.equal(url, 'https://api.github.com/app/installations/12345/access_tokens');
    assert.equal(init.method, 'POST');
    assert.deepEqual(JSON.parse(String(init.body)), { repositories: ['evals'], permissions: { contents: 'write' } });
    const { payload } = await jwtVerify(headers.get('Authorization')!.slice(7), publicKey, {
      algorithms: ['RS256'], issuer: env.GITHUB_APP_ID, currentDate: new Date(now),
    });
    assert.equal(payload.iat, Math.floor(now / 1000) - 60);
    assert.equal(payload.exp, Math.floor(now / 1000) + 540);
    exchanges++;
    return Response.json({ token: `installation-${exchanges}`, expires_at: new Date(now + 3_600_000).toISOString() });
  });
  assert.equal(githubConfigured(env), true);
  assert.equal(await githubToken(env), 'installation-1');
  assert.equal(await githubToken(env), 'installation-1');
  const result = await createTaskCommit(env, 'a'.repeat(40), [{ path: 'tasks/demo/instruction.md', content: 'A task' }], [], 'adds demo task');
  assert.equal(result.sha, 'b'.repeat(40));
  assert.equal(exchanges, 1);
  now += 3_550_000;
  assert.equal(await githubToken(env), 'installation-2');
  assert.equal(exchanges, 2);
});

test('PKCS8 and escaped PEM keys work and changing repository cannot reuse a cached token', async (t) => {
  const env = { ...appEnv('123'), GITHUB_APP_PRIVATE_KEY: pkcs8.replaceAll('\n', '\\n') };
  const repositories: string[] = [];
  t.mock.method(globalThis, 'fetch', async (_url: string, init: RequestInit) => {
    const repo = JSON.parse(String(init.body)).repositories[0];
    repositories.push(repo);
    return Response.json({ token: `token-${repo}`, expires_at: new Date(Date.now() + 3_600_000).toISOString() });
  });
  assert.equal(await githubToken(env), 'token-evals');
  assert.equal(await githubToken({ ...env, GITHUB_REPOSITORY: 'company/other-evals' }), 'token-other-evals');
  assert.deepEqual(repositories, ['evals', 'other-evals']);
});

test('bad keys and failed or expired exchanges never expose provider details or use the PAT fallback', async (t) => {
  const env = { ...appEnv('456'), GITHUB_TOKEN: 'must-not-fallback' };
  let fetches = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    fetches++;
    if (fetches === 1) return Response.json({ message: 'provider-private-detail' }, { status: 403 });
    if (fetches === 2) return Response.json({ token: 'expired', expires_at: new Date(Date.now() - 1).toISOString() });
    throw new Error('provider-private-detail');
  });
  await assert.rejects(githubToken({ ...env, GITHUB_APP_PRIVATE_KEY: 'secret-invalid-pem' }), /must contain the unencrypted RSA PEM key/);
  assert.equal(fetches, 0);
  for (const expected of [/authentication failed \(403\)/, /invalid installation token/, /Could not reach GitHub/]) {
    await assert.rejects(githubToken(env), (error: Error) => {
      assert.match(error.message, expected);
      assert.doesNotMatch(error.message, /provider-private-detail|secret-invalid-pem|must-not-fallback/);
      return true;
    });
  }
});
