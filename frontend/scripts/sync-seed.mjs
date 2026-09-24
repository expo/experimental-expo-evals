import { readFile, writeFile } from 'node:fs/promises';
const files = ['instruction.md', 'tests/rubric.toml', 'task.toml', 'environment/Dockerfile', 'environment/setup.sh', 'tests/test.sh'];
const task = { id: 'coffee-timer', name: 'Coffee timer', files: await Promise.all(files.map(async (path) => ({ path: `tasks/coffee-timer/${path}`, content: await readFile(new URL(`../../tasks/coffee-timer/${path}`, import.meta.url), 'utf8') }))) };
await writeFile(new URL('../src/lib/seed-task.ts', import.meta.url), `// Generated from native task files by npm run seed:sync. No credentials.\nexport const seedTask = ${JSON.stringify(task, null, 2)};\n`);
console.log('Updated the bundled coffee-timer seed. Existing D1 drafts are unchanged.');
