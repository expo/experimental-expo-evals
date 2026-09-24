import { createFileRoute } from '@tanstack/react-router';
import { context } from '../lib/cloud-context';
import { artifactResponse } from '../lib/cloud-runs';
export const Route = createFileRoute('/api/artifact')({ server: { handlers: { GET: ({ request }) => {
  const params = new URL(request.url).searchParams;
  return artifactResponse(context(), params.get('job') || '', params.get('path') || '');
} } } });
