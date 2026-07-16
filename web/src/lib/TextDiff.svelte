<script lang="ts">
  type DiffPart = { kind: 'same' | 'add' | 'remove'; text: string };
  let { before = '', after = '' }: { before: string; after: string } = $props();

  function coalesce(parts: DiffPart[]) {
    const result: DiffPart[] = [];
    for (const part of parts) {
      if (!part.text) continue;
      const previous = result.at(-1);
      if (previous?.kind === part.kind) previous.text += part.text;
      else result.push({ ...part });
    }
    return result;
  }

  function lcsDiff(left: string[], right: string[]): DiffPart[] {
    const rows = Array.from({ length: left.length + 1 }, () => new Uint32Array(right.length + 1));
    for (let i = left.length - 1; i >= 0; i -= 1) {
      for (let j = right.length - 1; j >= 0; j -= 1) rows[i][j] = left[i] === right[j] ? rows[i + 1][j + 1] + 1 : Math.max(rows[i + 1][j], rows[i][j + 1]);
    }
    const parts: DiffPart[] = [];
    let i = 0; let j = 0;
    while (i < left.length || j < right.length) {
      if (i < left.length && j < right.length && left[i] === right[j]) { parts.push({ kind: 'same', text: left[i] }); i += 1; j += 1; }
      else if (j < right.length && (i >= left.length || rows[i][j + 1] >= rows[i + 1][j])) { parts.push({ kind: 'add', text: right[j] }); j += 1; }
      else { parts.push({ kind: 'remove', text: left[i] }); i += 1; }
    }
    return coalesce(parts);
  }

  function fallbackDiff(left: string, right: string): DiffPart[] {
    let prefix = 0;
    while (prefix < left.length && prefix < right.length && left[prefix] === right[prefix]) prefix += 1;
    let suffix = 0;
    while (suffix < left.length - prefix && suffix < right.length - prefix && left[left.length - 1 - suffix] === right[right.length - 1 - suffix]) suffix += 1;
    return coalesce([
      { kind: 'same', text: left.slice(0, prefix) },
      { kind: 'remove', text: left.slice(prefix, left.length - suffix) },
      { kind: 'add', text: right.slice(prefix, right.length - suffix) },
      { kind: 'same', text: suffix ? right.slice(right.length - suffix) : '' }
    ]);
  }

  function buildDiff(left: string, right: string) {
    if (left === right) return [{ kind: 'same', text: left }] as DiffPart[];
    let leftTokens: string[] = left.match(/\s+|[^\s]+/g) ?? [];
    let rightTokens: string[] = right.match(/\s+|[^\s]+/g) ?? [];
    if (leftTokens.length * rightTokens.length > 250_000) {
      leftTokens = left.match(/.*(?:\r?\n|$)/g)?.filter(Boolean) ?? [left];
      rightTokens = right.match(/.*(?:\r?\n|$)/g)?.filter(Boolean) ?? [right];
    }
    return leftTokens.length * rightTokens.length <= 250_000 ? lcsDiff(leftTokens, rightTokens) : fallbackDiff(left, right);
  }

  const parts = $derived(buildDiff(before, after));
</script>

<div class="diff" aria-label="Text differences">
  {#each parts as part}<span class:add={part.kind === 'add'} class:remove={part.kind === 'remove'}>{part.text}</span>{/each}
</div>

<style>
  .diff{min-height:8rem;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:.8rem;background:var(--paper-strong);padding:1rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;line-height:1.65}
  .add{border-radius:.2rem;background:color-mix(in srgb,#22c55e 22%,transparent);color:color-mix(in srgb,#15803d 85%,var(--ink));text-decoration:none}
  .remove{border-radius:.2rem;background:color-mix(in srgb,#ef4444 20%,transparent);color:color-mix(in srgb,#b91c1c 85%,var(--ink));text-decoration:line-through;text-decoration-thickness:.08em}
</style>
