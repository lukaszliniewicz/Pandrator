/**
 * Remove Pandrator's resolver prefix while preserving the provider's native
 * model identifier. Native identifiers may themselves contain slashes.
 */
export function modelDisplayName(value: string) {
  return value
    .split(' · ')
    .map((item) => item.trim().replace(/^custom:[^/]+\//i, ''))
    .join(' · ');
}
