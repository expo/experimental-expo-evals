import { test } from 'node:test';
import assert from 'node:assert/strict';
import { authenticate, sameOriginMutation } from './cloud-auth.ts';
import type { CloudEnv } from './cloud-types';

test('local development bypass requires both the explicit flag and a loopback URL', async () => {
  assert.equal(await authenticate(new Request('http://127.0.0.1:3000'), { LOCAL_DEV: 'true' } as CloudEnv), 'local-developer');
  await assert.rejects(authenticate(new Request('https://evals.example.com'), { LOCAL_DEV: 'true' } as CloudEnv), /sign-in is not configured/);
  await assert.rejects(authenticate(new Request('http://127.0.0.1:3000'), {} as CloudEnv), /sign-in is not configured/);
});

test('production authentication fails closed and does not trust a claimed email header', async () => {
  const env = { ACCESS_TEAM_DOMAIN: 'https://company.cloudflareaccess.com', ACCESS_AUD: 'application-aud' } as CloudEnv;
  const request = new Request('https://evals.example.com', { headers: { 'Cf-Access-Authenticated-User-Email': 'someone@company.com' } });
  await assert.rejects(authenticate(request, env), /Sign in through/);
  await assert.rejects(authenticate(request, { ...env, ACCESS_TEAM_DOMAIN: 'https://attacker.example.com' }), /not configured/);
});

test('writes require the browser origin to match the app, including requests without Origin', () => {
  assert.equal(sameOriginMutation(new Request('https://evals.example.com/api', { method: 'POST', headers: { Origin: 'https://evil.example.com' } })), false);
  assert.equal(sameOriginMutation(new Request('https://evals.example.com/api', { method: 'POST' })), false);
  assert.equal(sameOriginMutation(new Request('https://evals.example.com/api', { method: 'POST', headers: { Origin: 'https://evals.example.com' } })), true);
  assert.equal(sameOriginMutation(new Request('https://evals.example.com/')), true);
});
