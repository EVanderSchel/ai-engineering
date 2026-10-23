// @vitest-environment node
import { readFile } from 'node:fs/promises';
import { generate } from '../../scripts/types.mjs';

const lf = (text: string) => text.replaceAll('\r\n', '\n'); // git may check files out with CRLF on Windows

test('the TypeScript types are generated from the current schema', async () => {
  const committed = await readFile(new URL('./types.ts', import.meta.url), 'utf8');
  expect(lf(committed), 'run `npm run types`').toBe(lf(await generate()));
});
