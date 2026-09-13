/** Shared top-layer help behavior for setting help and interactive block details. */
export function helpPopover(
  anchor: () => HTMLElement | undefined,
  popup: () => HTMLElement | undefined,
  scope: () => HTMLElement | undefined = anchor
) {
  let hideTimer: ReturnType<typeof setTimeout> | undefined;
  let anchorRect: DOMRect | undefined;

  function hide() {
    clearTimeout(hideTimer);
    if (typeof window === 'undefined') return;
    const node = popup();
    if (node?.matches(':popover-open')) node.hidePopover();
    anchorRect = undefined;
    window.removeEventListener('scroll', onScroll, true);
    window.removeEventListener('resize', hide);
    document.removeEventListener('pointerdown', outside);
  }

  function outside(event: PointerEvent) {
    if (event.target instanceof Node && !scope()?.contains(event.target))
      hide();
  }

  function onScroll(event: Event) {
    if (event.target instanceof Node && popup()?.contains(event.target)) return;
    const rect = anchor()?.getBoundingClientRect();
    // A scroll queued before opening, or in another panel, need not dismiss help.
    if (
      rect &&
      anchorRect &&
      rect.top === anchorRect.top &&
      rect.left === anchorRect.left &&
      rect.bottom === anchorRect.bottom &&
      rect.right === anchorRect.right
    )
      return;
    hide();
  }

  function show() {
    clearTimeout(hideTimer);
    const node = popup();
    const trigger = anchor();
    if (!node || !trigger || node.matches(':popover-open')) return;
    node.showPopover();
    const rect = trigger.getBoundingClientRect();
    anchorRect = rect;
    const size = node.getBoundingClientRect();
    node.style.left = `${Math.max(8, Math.min(rect.right - size.width, window.innerWidth - size.width - 8))}px`;
    node.style.top = `${Math.max(8, Math.min(rect.bottom + size.height + 8 <= window.innerHeight ? rect.bottom + 6 : rect.top - size.height - 6, window.innerHeight - size.height - 8))}px`;
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', hide);
    document.addEventListener('pointerdown', outside);
  }

  function leave() {
    hideTimer = setTimeout(() => {
      if (!scope()?.contains(document.activeElement)) hide();
    }, 120);
  }

  function focusout(event: FocusEvent) {
    if (
      !(event.relatedTarget instanceof Node) ||
      !scope()?.contains(event.relatedTarget)
    )
      hide();
  }

  function escape(event: KeyboardEvent) {
    if (event.key === 'Escape' && popup()?.matches(':popover-open')) {
      event.preventDefault();
      event.stopPropagation();
      hide();
    }
  }

  return { show, hide, leave, focusout, escape };
}
