import { timingSafeEqual } from 'node:crypto';
import { createRemoteJWKSet, jwtVerify, SignJWT } from 'jose';
import type { CloudEnv } from './cloud-types';

const keySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();
const sessionCookie = 'eas-evals-session';
const sessionSeconds = 12 * 60 * 60;
const encoder = new TextEncoder();

function isLoopback(request: Request): boolean {
  return ['localhost', '127.0.0.1', '[::1]'].includes(new URL(request.url).hostname);
}

function accessConfigured(env: CloudEnv): boolean {
  return Boolean(env.ACCESS_TEAM_DOMAIN || env.ACCESS_AUD);
}

function sessionConfiguration(request: Request, env: CloudEnv) {
  if (!env.DASHBOARD_USERNAME || !env.DASHBOARD_PASSWORD || !env.SESSION_SECRET || env.SESSION_SECRET.length < 32) {
    throw new Error('Dashboard sign-in is not configured.');
  }
  const origin = new URL(env.APP_ORIGIN || request.url).origin;
  return { key: encoder.encode(env.SESSION_SECRET), issuer: origin, audience: `${origin}/dashboard` };
}

export async function authenticate(request: Request, env: CloudEnv): Promise<string> {
  // LOCAL_DEV is only set in the ignored .dev.vars file. Deployment defaults to false.
  if (env.LOCAL_DEV === 'true' && isLoopback(request)) return 'local-developer';
  if (accessConfigured(env)) {
    const team = env.ACCESS_TEAM_DOMAIN?.replace(/\/$/, '');
    if (!team || !/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(team) || !env.ACCESS_AUD) {
      throw new Error('Company sign-in is not configured.');
    }
    const token = request.headers.get('Cf-Access-Jwt-Assertion');
    if (!token) throw new Error('Sign in through your company Cloudflare Access application.');
    let keys = keySets.get(team);
    if (!keys) { keys = createRemoteJWKSet(new URL(`${team}/cdn-cgi/access/certs`)); keySets.set(team, keys); }
    try {
      const { payload } = await jwtVerify(token, keys, { issuer: team, audience: env.ACCESS_AUD, algorithms: ['RS256'] });
      if (typeof payload.email !== 'string' || !payload.email) throw new Error('No email');
      return payload.email;
    } catch { throw new Error('Your company sign-in is invalid or expired. Sign in again.'); }
  }
  const config = sessionConfiguration(request, env);
  const token = request.headers.get('Cookie')?.split(';').map(part => part.trim()).find(part => part.startsWith(`${sessionCookie}=`))?.slice(sessionCookie.length + 1);
  if (!token) throw new Error('Sign in to the dashboard.');
  try {
    const { payload } = await jwtVerify(token, config.key, {
      issuer: config.issuer, audience: config.audience, algorithms: ['HS256'], maxTokenAge: sessionSeconds,
    });
    if (typeof payload.sub !== 'string' || payload.sub !== env.DASHBOARD_USERNAME || typeof payload.exp !== 'number') throw new Error('Invalid session');
    return payload.sub;
  } catch { throw new Error('Your session is invalid or expired. Sign in again.'); }
}

export function sameOriginMutation(request: Request): boolean {
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method)) return true;
  return request.headers.get('origin') === new URL(request.url).origin;
}

function safeDestination(value: string | null): string {
  return value && value.startsWith('/') && !value.startsWith('//') && !/[\\\r\n]/.test(value) && !value.startsWith('/login') && !value.startsWith('/logout') ? value : '/';
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]!);
}

function loginPage(destination: string, error = '', status = 200): Response {
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · EAS evals</title><style>
  *{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:#17212b;font:15px system-ui,sans-serif;display:grid;min-height:100svh;place-items:center;padding:24px}main{width:100%;max-width:360px}h1{font-size:25px;letter-spacing:-.6px;margin:0 0 8px}p{line-height:1.5;color:#637080;margin:0 0 28px}label{display:block;font-size:13px;font-weight:600;margin:18px 0 7px}input,button{width:100%;border:1px solid #d8dfe7;border-radius:8px;padding:11px 12px;font:inherit}input{background:white}input:focus-visible,button:focus-visible{outline:3px solid #8cb8fa;outline-offset:2px}button{margin-top:22px;background:#17212b;color:white;border-color:#17212b;cursor:pointer;font-weight:600}.error{color:#a51f27;margin:18px 0 0;font-size:14px}
  </style></head><body><main><h1>EAS evals</h1><p>Sign in to create and run evaluations.</p><form method="post" action="/login"><input type="hidden" name="next" value="${escapeHtml(destination)}"><label for="username">Username</label><input id="username" name="username" autocomplete="username" maxlength="256" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="4096" required>${error ? `<p class="error" role="alert">${escapeHtml(error)}</p>` : ''}<button type="submit">Sign in</button></form></main></body></html>`, {
    // no-referrer turns native form POST origins into "null", failing the CSRF check.
    status, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin' },
  });
}

function cookie(request: Request, env: CloudEnv, token: string, seconds: number): string {
  const secure = new URL(request.url).protocol === 'https:' || !(env.LOCAL_DEV === 'true' && isLoopback(request));
  return `${sessionCookie}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${seconds}${secure ? '; Secure' : ''}`;
}

async function equalSecret(actual: string, expected: string): Promise<boolean> {
  const [a, b] = await Promise.all([actual, expected].map(value => crypto.subtle.digest('SHA-256', encoder.encode(value))));
  return timingSafeEqual(new Uint8Array(a), new Uint8Array(b));
}

async function loginBody(request: Request): Promise<string> {
  if (!request.body) return '';
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 8192) { await reader.cancel(); throw new Error('Login form is too large.'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(bytes);
}

/** These two routes are handled before the authenticated TanStack application. */
export async function authenticationRoute(request: Request, env: CloudEnv): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname !== '/login' && url.pathname !== '/logout') return null;
  const headers = { 'Cache-Control': 'no-store' };
  if (!sameOriginMutation(request)) return new Response('Cross-origin writes are not allowed', { status: 403, headers });
  if (url.pathname === '/logout') {
    if (request.method !== 'POST') return new Response('Use the sign out button.', { status: 405, headers: { ...headers, Allow: 'POST' } });
    return new Response(null, { status: 303, headers: { ...headers, Location: accessConfigured(env) ? '/cdn-cgi/access/logout' : '/login', 'Set-Cookie': cookie(request, env, '', 0) } });
  }
  if (accessConfigured(env)) return new Response('Sign in through your company Cloudflare Access application.', { status: 403, headers });
  let config: ReturnType<typeof sessionConfiguration>;
  try { config = sessionConfiguration(request, env); }
  catch { return new Response('Dashboard sign-in is not configured.', { status: 503, headers }); }
  if (request.method === 'GET') {
    const destination = safeDestination(url.searchParams.get('next'));
    try { await authenticate(request, env); return new Response(null, { status: 303, headers: { ...headers, Location: destination } }); }
    catch { return loginPage(destination); }
  }
  if (request.method !== 'POST') return new Response('Method not allowed', { status: 405, headers: { ...headers, Allow: 'GET, POST' } });
  if (!request.headers.get('Content-Type')?.startsWith('application/x-www-form-urlencoded')) return new Response('Invalid login form.', { status: 400, headers });
  let form: URLSearchParams;
  try { form = new URLSearchParams(await loginBody(request)); }
  catch { return new Response('Login form is too large.', { status: 413, headers }); }
  const destination = safeDestination(form.get('next'));
  const [usernameMatches, passwordMatches] = await Promise.all([
    equalSecret(form.get('username') || '', env.DASHBOARD_USERNAME!),
    equalSecret(form.get('password') || '', env.DASHBOARD_PASSWORD!),
  ]);
  if (!usernameMatches || !passwordMatches) return loginPage(destination, 'Incorrect username or password.', 401);
  const token = await new SignJWT({}).setProtectedHeader({ alg: 'HS256' }).setSubject(env.DASHBOARD_USERNAME!).setIssuer(config.issuer).setAudience(config.audience).setIssuedAt().setExpirationTime(`${sessionSeconds}s`).sign(config.key);
  return new Response(null, { status: 303, headers: { ...headers, Location: destination, 'Set-Cookie': cookie(request, env, token, sessionSeconds) } });
}

export function authenticationFailure(request: Request, error: unknown): Response {
  const url = new URL(request.url);
  const headers = { 'Cache-Control': 'no-store' };
  if (request.method === 'GET' && request.headers.get('Accept')?.includes('text/html') && !url.pathname.startsWith('/api/') && !url.pathname.startsWith('/_serverFn/')) {
    return new Response(null, { status: 303, headers: { ...headers, Location: `/login?next=${encodeURIComponent(url.pathname + url.search)}` } });
  }
  return new Response(error instanceof Error ? error.message : 'Sign-in required', { status: 403, headers });
}
