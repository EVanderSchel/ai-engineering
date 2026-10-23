// @vitest-environment node
import { readFile } from 'node:fs/promises';
import { generate } from '../../scripts/types.mjs';

test('the TypeScript types are generated from the current schema', async () => {
  const committed = await readFile(new URL('./types.ts', import.meta.url), 'utf8');
  expect(committed, 'run `npm run types`').toBe(await generate());
});
