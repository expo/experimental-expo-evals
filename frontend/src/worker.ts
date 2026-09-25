import { createStartHandler, defaultStreamHandler } from '@tanstack/react-start/server';
import { requestContext } from './lib/cloud-context';
import { authenticate, authenticationFailure, authenticationRoute, sameOriginMutation } from './lib/cloud-auth';
import { expireStaleRuns, runnerCallback } from './lib/cloud-runs';
import type { CloudEnv } from './lib/cloud-types';
import type { HarborRunner } from './lib/runner-container';
export { HarborRunner } from './lib/runner-container';

const handle = createStartHandler(defaultStreamHandler);
export default {
  async fetch(request: Request, env: CloudEnv, execution: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const callback = /^\/api\/runner-callback\/([a-zA-Z0-9_-]+)$/.exec(url.pathname);
    if (callback) {
      const response = await runnerCallback(request, env, callback[1]);
      if (response.ok && response.headers.get('x-run-complete') === '1') {
        // Results are already durable before asking the disposable runner to stop.
        const runners = env.RUNNERS as unknown as DurableObjectNamespace<HarborRunner>;
        execution.waitUntil(runners.get(runners.idFromName(callback[1])).stop().catch(() => {}));
      }
      return response;
    }
    const login = await authenticationRoute(request, env);
    if (login) return login;
    let user: string;
    try { user = await authenticate(request, env); }
    catch (error) { return authenticationFailure(request, error); }
    if (!sameOriginMutation(request)) return new Response('Cross-origin writes are not allowed', { status: 403 });
    const response = await requestContext.run({ env, user }, () => handle(request));
    const headers = new Headers(response.headers);
    headers.set('Cache-Control', 'no-store');
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
  },
  async scheduled(_controller: ScheduledController, env: CloudEnv) { await expireStaleRuns(env); },
} satisfies ExportedHandler<CloudEnv>;
