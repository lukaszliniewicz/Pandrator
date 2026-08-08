import type { Action } from 'svelte/action';

export type ModalFocusOptions = {
  onclose?: () => void;
  closeOnEscape?: boolean;
  initialFocus?: string;
};

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'summary',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])'
].join(',');

function focusableChildren(node: HTMLElement) {
  return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => {
      const style = getComputedStyle(element);
      return (
        !element.hidden &&
        element.getAttribute('aria-hidden') !== 'true' &&
        element.getClientRects().length > 0 &&
        style.display !== 'none' &&
        style.visibility !== 'hidden'
      );
    }
  );
}

/**
 * Supplies the keyboard behavior expected of an ARIA modal dialog:
 * initial focus, Tab containment, Escape-to-close, and focus restoration.
 */
export const modalFocus: Action<HTMLElement, ModalFocusOptions | undefined> = (
  node,
  initialOptions
) => {
  let options = initialOptions ?? {};
  const returnFocus =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  const hadTabIndex = node.hasAttribute('tabindex');

  if (!hadTabIndex) node.tabIndex = -1;

  function focusInitial() {
    const requested = options.initialFocus
      ? node.querySelector<HTMLElement>(options.initialFocus)
      : null;
    (requested ?? focusableChildren(node)[0] ?? node).focus({
      preventScroll: true
    });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (
      event.key === 'Escape' &&
      options.closeOnEscape !== false &&
      options.onclose
    ) {
      event.preventDefault();
      event.stopPropagation();
      options.onclose();
      return;
    }
    if (event.key !== 'Tab') return;

    const focusable = focusableChildren(node);
    if (!focusable.length) {
      event.preventDefault();
      node.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable.at(-1)!;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !node.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !node.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  node.addEventListener('keydown', handleKeydown);
  queueMicrotask(focusInitial);

  return {
    update(nextOptions) {
      options = nextOptions ?? {};
    },
    destroy() {
      node.removeEventListener('keydown', handleKeydown);
      if (!hadTabIndex) node.removeAttribute('tabindex');
      queueMicrotask(() => {
        if (returnFocus?.isConnected)
          returnFocus.focus({ preventScroll: true });
      });
    }
  };
};
