import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, statSync } from 'node:fs';

const packageMetadata = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8')
);
const versionHash = createHash('sha256');

function hashVersionInput(relativePath) {
  const input = new URL(relativePath, import.meta.url);
  if (statSync(input).isDirectory()) {
    for (const entry of readdirSync(input).sort()) {
      hashVersionInput(`${relativePath}/${entry}`);
    }
    return;
  }

  versionHash.update(relativePath);
  versionHash.update('\0');
  versionHash.update(readFileSync(input));
}

for (const input of [
  './package.json',
  './package-lock.json',
  './svelte.config.js',
  './tsconfig.json',
  './vite.config.ts',
  './src',
  './static'
]) {
  hashVersionInput(input);
}

const sourceVersion = versionHash.digest('hex').slice(0, 12);

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    version: {
      name: `${packageMetadata.version}-${sourceVersion}`
    },
    adapter: adapter({
      pages: '../pandrator/web/static',
      assets: '../pandrator/web/static',
      fallback: 'index.html',
      precompress: false,
      strict: true
    })
  }
};

export default config;
