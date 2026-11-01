import { beforeNavigate, goto } from '$app/navigation';
import { onMount } from 'svelte';

/** Register from an editor's component setup; the controller owns its draft. */
export function guardEditorNavigation(options: {
  dirty: () => boolean;
  pending: () => boolean;
  requestLeave: (action: () => void) => void;
}) {
  let permitted = false;
  beforeNavigate((navigation) => {
    if (permitted || (!options.dirty() && !options.pending())) return;
    navigation.cancel();
    if (navigation.type === 'leave' || options.pending()) return;
    const destination = navigation.to?.url;
    if (!destination) return;
    options.requestLeave(() => {
      permitted = true;
      void goto(destination, {
        replaceState: navigation.type === 'popstate'
      }).catch(() => {
        permitted = false;
      });
    });
  });
  onMount(() => {
    const unload = (event: BeforeUnloadEvent) => {
      if (options.dirty() || options.pending()) event.preventDefault();
    };
    window.addEventListener('beforeunload', unload);
    return () => window.removeEventListener('beforeunload', unload);
  });
}
