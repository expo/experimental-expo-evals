import { AsyncLocalStorage } from 'node:async_hooks';
import type { CloudContext } from './cloud-types';

export const requestContext = new AsyncLocalStorage<CloudContext>();
export function context(): CloudContext {
  const value = requestContext.getStore();
  if (!value) throw new Error('Authenticated request context is missing.');
  return value;
}
