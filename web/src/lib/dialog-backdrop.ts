import type { Action } from 'svelte/action';

/** A click that began on the backdrop follows the same close guard as Escape. */
export const dialogBackdrop: Action<HTMLDialogElement, () => void> = (
  node,
  close
) => {
  let requestClose = close;
  const pointer = (event: PointerEvent) => {
    if (event.target !== node || event.button !== 0) return;
    const rect = node.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      requestClose();
  };
  node.addEventListener('pointerdown', pointer);
  return {
    update(next) {
      requestClose = next;
    },
    destroy() {
      node.removeEventListener('pointerdown', pointer);
    }
  };
};
