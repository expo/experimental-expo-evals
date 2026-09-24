import { readFile, writeFile } from 'node:fs/promises';
import { parse } from 'dotenv';
const path = new URL('../.dev.vars', import.meta.url);
const old = await readFile(path, 'utf8').catch(() => '');
const values = { LOCAL_DEV: 'true', ...parse(old) };
const source = parse(await readFile(new URL('../../.env', import.meta.url), 'utf8'));
for (const key of ['ANTHROPIC_API_KEY', 'E2B_API_KEY', 'EXPO_TOKEN', 'TYPESAFE_API_KEY', 'OPENAI_API_KEY', 'GITHUB_TOKEN']) {
  if (!values[key] && source[key]) values[key] = source[key];
}
await writeFile(path, Object.entries(values).map(([key, value]) => `${key}=${JSON.stringify(value)}`).join('\n') + '\n', { mode: 0o600 });
console.log('Prepared ignored .dev.vars from existing local credentials. No values were printed.');
