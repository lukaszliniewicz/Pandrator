import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
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

function crlfCopies() {
  let listing;
  try {
    listing = execFileSync('git', ['ls-files', '--eol', '--', ...lfPaths], {
      cwd: fileURLToPath(new URL('../../', import.meta.url)),
      encoding: 'utf8',
      maxBuffer: 16 * 1024 * 1024,
      stdio: ['ignore', 'pipe', 'ignore']
    });
  } catch {
    return []; // No git, or not a work tree: nothing to compare against.
  }
  return listing
    .split('\n')
    .filter((line) => /\sw\/crlf\s/.test(line))
    .map((line) => line.slice(line.indexOf('\t') + 1));
}

const crlf = crlfCopies();
if (crlf.length > 0) {
  console.error(
    `${crlf.length} tracked build files have CRLF line endings in this checkout, for example ${crlf[0]}.`
  );
  console.error(
    'Builds from them differ from CI. Do not rebuild or commit assets from this checkout.'
  );
  console.error(
    `Refresh it instead: with no changes under those paths, delete their tracked files and run git checkout -- ${lfPaths.join(' ')}`
  );
  process.exitCode = 1;
} else {
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
