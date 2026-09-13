import type { Action } from 'svelte/action';
import { modalFocus, type ModalFocusOptions } from './modal-focus';

/** Put the dialog above drawers and scrolling panels using the browser's top layer. */
export const modalDialog: Action<HTMLDialogElement, ModalFocusOptions> = (
  node,
  options
) => {
  const focus = modalFocus(node, options);
  const cancel = (event: Event) => event.preventDefault();
  node.addEventListener('cancel', cancel);
  node.showModal();
  return {
    update(next) {
      focus?.update?.(next);
    },
    destroy() {
      node.removeEventListener('cancel', cancel);
      node.close();
      focus?.destroy?.();
    }
  };
};
