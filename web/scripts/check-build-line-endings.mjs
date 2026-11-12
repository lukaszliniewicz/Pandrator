import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';

const TEXT_ASSET_EXTENSIONS = new Set([
  '.css',
  '.html',
  '.js',
  '.json',
  '.map',
  '.mjs',
  '.svg',
  '.txt',
  '.xml'
]);

// -z preserves unusual (including whitespace/newline-containing) Git paths.
export function badTrackedWorkingTreeEols(listing) {
  return listing
    .split('\0')
    .filter(Boolean)
    .flatMap((entry) => {
      const tab = entry.indexOf('\t');
      if (tab < 0) throw new Error('Unexpected git ls-files --eol output.');
      const details = entry.slice(0, tab);
      return /(?:^|\s)w\/(?:crlf|mixed)(?=\s|$)/.test(details)
        ? [entry.slice(tab + 1)]
        : [];
    });
}

export function trackedEolProblems(root, paths, { git = execFileSync } = {}) {
  // Published source packages may have no .git. An actual checkout must
  // successfully prove its file state; a broken git command is not success.
  if (!existsSync(join(root, '.git'))) return [];
  return badTrackedWorkingTreeEols(
    git('git', ['ls-files', '--eol', '-z', '--', ...paths], {
      cwd: root,
      encoding: 'utf8',
      maxBuffer: 16 * 1024 * 1024,
      stdio: ['ignore', 'pipe', 'pipe']
    })
  );
}

export function generatedCrLfAssets(staticDir) {
  const problems = [];
  if (!existsSync(staticDir)) return problems;
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(path);
      } else if (entry.isFile()) {
        const extension = entry.name
          .slice(entry.name.lastIndexOf('.'))
          .toLowerCase();
        if (!TEXT_ASSET_EXTENSIONS.has(extension)) continue;
        if (readFileSync(path).includes(Buffer.from('\r\n'))) {
          problems.push(relative(staticDir, path));
        }
      }
    }
  };
  walk(staticDir);
  return problems;
}
