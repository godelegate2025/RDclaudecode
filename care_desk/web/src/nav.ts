import { useSyncExternalStore, type MouseEvent } from 'react';

/* Each view has its own address (/inbox, /email/oldlead…), and the server sends the same
   page for all of them. Moving between views changes the address without reloading the
   page, and Back and Forward move between them too. */
const listeners = new Set<() => void>();
const changed = () => listeners.forEach(l => l());
window.addEventListener('popstate', changed);

export function go(path: string) {
  if (path === location.pathname) return;
  history.pushState(null, '', path);
  window.scrollTo(0, 0);
  changed();
}

export const usePath = () => useSyncExternalStore(
  l => { listeners.add(l); return () => { listeners.delete(l); }; },
  () => location.pathname,
);

/** For a link to a view: a plain click stays in the page; Ctrl/⌘-click or a middle click still opens a new tab. */
export function onLink(e: MouseEvent<HTMLAnchorElement>) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  go(e.currentTarget.getAttribute('href')!);
}
