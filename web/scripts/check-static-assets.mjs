import { readFileSync } from 'node:fs';
import config from '../svelte.config.js';

const versionFile = new URL(
  '../../pandrator/web/static/_app/version.json',
  import.meta.url
);

try {
  const actual = JSON.parse(readFileSync(versionFile, 'utf8')).version;
  const expected = config.kit.version.name;
  if (actual !== expected) {
    throw new Error(`Expected ${expected}, found ${String(actual)}.`);
  }
  console.log(`Browser assets match source: ${actual}`);
} catch (error) {
  console.error(
    'Browser assets are missing or do not match the current source.'
  );
  console.error(error instanceof Error ? error.message : String(error));
  console.error(
    'Run npm --prefix web run build and commit the generated assets.'
  );
  process.exitCode = 1;
}
