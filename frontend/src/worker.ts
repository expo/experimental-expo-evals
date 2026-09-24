import { createStartHandler, defaultStreamHandler } from '@tanstack/react-start/server';
import { requestContext } from './lib/cloud-context';
import { authenticate, sameOriginMutation } from './lib/cloud-auth';
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
    let user: string;
    try { user = await authenticate(request, env); }
    catch (error) { return new Response(error instanceof Error ? error.message : 'Sign-in required', { status: 403, headers: { 'Cache-Control': 'no-store' } }); }
    if (!sameOriginMutation(request)) return new Response('Cross-origin writes are not allowed', { status: 403 });
    return requestContext.run({ env, user }, () => handle(request));
  },
  async scheduled(_controller: ScheduledController, env: CloudEnv) { await expireStaleRuns(env); },
} satisfies ExportedHandler<CloudEnv>;
