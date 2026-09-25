import { readFile, writeFile } from "node:fs/promises";

const template = await readFile(
  new URL("../../eas_provider/workflow.yml", import.meta.url),
  "utf8",
);
await writeFile(
  new URL("../src/lib/eas-workflow.ts", import.meta.url),
  `// Generated from eas_provider/workflow.yml before dev/build.\n// prettier-ignore\nexport const easWorkflowTemplate = ${JSON.stringify(template)};\n`,
);
