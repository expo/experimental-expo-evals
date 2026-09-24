import { createRemoteJWKSet, jwtVerify } from 'jose';
import type { CloudEnv } from './cloud-types';

const keySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();
export async function authenticate(request: Request, env: CloudEnv): Promise<string> {
  // LOCAL_DEV is only set in the ignored .dev.vars file. Deployment defaults to false.
  if (env.LOCAL_DEV === 'true') {
    const host = new URL(request.url).hostname;
    if (host === 'localhost' || host === '127.0.0.1' || host === '[::1]') return 'local-developer';
  }
  const team = env.ACCESS_TEAM_DOMAIN?.replace(/\/$/, '');
  if (!team || !/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(team) || !env.ACCESS_AUD) {
    throw new Error('Company sign-in is not configured. Set Cloudflare Access team domain and audience before sharing this app.');
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

export function sameOriginMutation(request: Request): boolean {
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method)) return true;
  return request.headers.get('origin') === new URL(request.url).origin;
}
