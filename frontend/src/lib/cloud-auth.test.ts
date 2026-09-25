import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SignJWT } from 'jose';
import { authenticate, authenticationFailure, authenticationRoute, sameOriginMutation } from './cloud-auth.ts';
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

const dashboardEnv = {
  DASHBOARD_USERNAME: 'team', DASHBOARD_PASSWORD: 'a-strong-shared-test-password',
  SESSION_SECRET: 'a-32-byte-minimum-session-secret-for-tests', APP_ORIGIN: 'https://evals.example.com',
} as CloudEnv;

function loginRequest(fields: Record<string, string> = {}, origin = 'https://evals.example.com') {
  return new Request('https://evals.example.com/login', {
    method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: dashboardEnv.DASHBOARD_USERNAME!, password: dashboardEnv.DASHBOARD_PASSWORD!, ...fields }),
  });
}

test('password login issues a private secure session and authenticates the dashboard', async () => {
  const response = (await authenticationRoute(loginRequest({ next: '/?task=coffee-timer' }), dashboardEnv))!;
  assert.equal(response.status, 303);
  assert.equal(response.headers.get('Location'), '/?task=coffee-timer');
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  const cookie = response.headers.get('Set-Cookie')!;
  assert.match(cookie, /HttpOnly; SameSite=Lax; Max-Age=43200; Secure/);
  assert.equal(await authenticate(new Request('https://evals.example.com', { headers: { Cookie: cookie.split(';')[0] } }), dashboardEnv), 'team');
  const page = (await authenticationRoute(new Request('https://evals.example.com/login'), dashboardEnv))!;
  assert.equal(page.status, 200);
  assert.match(await page.text(), /autocomplete="current-password"/);
});

test('wrong credentials and cross-origin login cannot create sessions', async () => {
  for (const fields of [{ password: 'wrong' }, { username: 'other' }] as Record<string, string>[]) {
    const response = (await authenticationRoute(loginRequest(fields), dashboardEnv))!;
    assert.equal(response.status, 401);
    assert.equal(response.headers.get('Set-Cookie'), null);
    assert.match(await response.text(), /Incorrect username or password/);
  }
  const forged = (await authenticationRoute(loginRequest({}, 'https://evil.example.com'), dashboardEnv))!;
  assert.equal(forged.status, 403);
  assert.equal(forged.headers.get('Set-Cookie'), null);
  const large = (await authenticationRoute(loginRequest({ password: 'x'.repeat(9000) }), dashboardEnv))!;
  assert.equal(large.status, 413);
});

test('sessions reject tampering, expiration, and tokens issued for a different site or user', async () => {
  const key = new TextEncoder().encode(dashboardEnv.SESSION_SECRET);
  const token = (options: { issuer?: string; subject?: string; issuedAt?: number; expiresAt?: number } = {}) => {
    const now = Math.floor(Date.now() / 1000);
    return new SignJWT({}).setProtectedHeader({ alg: 'HS256' }).setIssuer(options.issuer || 'https://evals.example.com').setAudience('https://evals.example.com/dashboard').setSubject(options.subject || 'team').setIssuedAt(options.issuedAt || now).setExpirationTime(options.expiresAt || now + 43200).sign(key);
  };
  const good = await token();
  const signatureStart = good.lastIndexOf('.') + 1;
  const tampered = good.slice(0, signatureStart) + (good[signatureStart] === 'a' ? 'b' : 'a') + good.slice(signatureStart + 1);
  const now = Math.floor(Date.now() / 1000);
  for (const value of [tampered, await token({ expiresAt: now - 10 }), await token({ issuedAt: now - 43201 }), await token({ issuer: 'https://evil.example.com' }), await token({ subject: 'other' })]) {
    await assert.rejects(authenticate(new Request('https://evals.example.com', { headers: { Cookie: `eas-evals-session=${value}` } }), dashboardEnv), /invalid or expired/);
  }
});

test('login prevents external redirects and logout clears the cookie only on same-origin POST', async () => {
  for (const next of ['//evil.example.com', '/\\evil.example.com', 'https://evil.example.com', '/login']) {
    const response = (await authenticationRoute(loginRequest({ next }), dashboardEnv))!;
    assert.equal(response.headers.get('Location'), '/');
  }
  const response = (await authenticationRoute(new Request('https://evals.example.com/logout', { method: 'POST', headers: { Origin: 'https://evals.example.com' } }), dashboardEnv))!;
  assert.equal(response.status, 303);
  assert.match(response.headers.get('Set-Cookie')!, /Max-Age=0; Secure/);
  assert.equal((await authenticationRoute(new Request('https://evals.example.com/logout'), dashboardEnv))!.status, 405);
  assert.equal((await authenticationRoute(new Request('https://evals.example.com/logout', { method: 'POST', headers: { Origin: 'https://evil.example.com' } }), dashboardEnv))!.status, 403);
});

test('anonymous pages redirect to login while APIs remain forbidden', () => {
  const response = authenticationFailure(new Request('https://evals.example.com/?task=coffee-timer', { headers: { Accept: 'text/html' } }), new Error('Sign in'));
  assert.equal(response.status, 303);
  assert.equal(response.headers.get('Location'), '/login?next=%2F%3Ftask%3Dcoffee-timer');
  for (const path of ['/api/artifact', '/_serverFn/example']) {
    assert.equal(authenticationFailure(new Request(`https://evals.example.com${path}`, { headers: { Accept: 'text/html' } }), new Error('Sign in')).status, 403);
  }
  assert.equal(authenticationFailure(new Request('https://evals.example.com/'), new Error('Sign in')).status, 403);
});
