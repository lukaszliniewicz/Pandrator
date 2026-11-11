import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  generatedCrLfAssets,
  trackedEolProblems
} from './check-build-line-endings.mjs';
import { fileURLToPath } from 'node:url';
import config from '../svelte.config.js';

const versionFile = new URL(
  '../../pandrator/web/static/_app/version.json',
  import.meta.url
);
// .gitattributes keeps these LF, but a checkout made before those rules keeps its CRLF
// copies. A build from them differs from CI's rebuild even when version.json matches.
const lfPaths = [
  'web',
  'pandrator/web/static',
  'pandrator_manager/recovery_ui/static/model-groups.js'
];

const root = fileURLToPath(new URL('../../', import.meta.url));
let badEols = [];
let eolCheckError = false;
try {
  badEols = [
    ...trackedEolProblems(root, lfPaths),
    ...generatedCrLfAssets(join(root, 'pandrator/web/static'))
  ];
} catch (error) {
  eolCheckError = true;
  console.error('Unable to verify browser build file line endings.');
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}

if (badEols.length > 0) {
  console.error(
    `${badEols.length} browser build files have CRLF or mixed line endings, for example ${badEols[0]}.`
  );
  console.error(
    'Builds from them differ from CI. Do not rebuild or commit assets from this checkout.'
  );
  console.error(
    'Review and preserve any local edits before refreshing the checkout; for generated assets, rebuild from LF sources.'
  );
  process.exitCode = 1;
} else if (!eolCheckError) {
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
}
