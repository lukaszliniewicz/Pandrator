/** Convert an unknown caught value to the message shown in frontend UI. */
export function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}
