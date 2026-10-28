import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { holdRefresh } from './desk';
import { Icon } from './icons';

/* ---------------------------------------------------------------- toasts */

type ToastKind = '' | 'u';
type Toast = { id: number; text: string; kind: ToastKind };
const ToastContext = createContext<(text: string, kind?: ToastKind) => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function Toasts({ children }: { children: ReactNode }) {
  const [list, setList] = useState<Toast[]>([]);
  const next = useRef(0);
  const show = useCallback((text: string, kind: ToastKind = '') => {
    const id = ++next.current;
    setList(l => [...l, { id, text, kind }].slice(-3));
    setTimeout(() => setList(l => l.filter(t => t.id !== id)), 4600);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      <div id="toasts" aria-live="polite">
        {list.map(t => (
          <div key={t.id} className={`toast ${t.kind}`}>
            <span className="ti"><Icon name={t.kind === 'u' ? 'alert' : 'check'} size={14} stroke={2.2} /></span>
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/* ---------------------------------------------------------------- windows */

/** Escape closes; focus moves in on open and back to where it was on close; the refresh waits meanwhile. */
function useLayer(onClose: () => void) {
  const ref = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const release = holdRefresh();
    const first = ref.current?.querySelector<HTMLElement>('input:not([type=checkbox]):not([type=file]), textarea');
    (first || ref.current)?.focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') close.current(); };
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('keydown', key);
      release();
      before?.focus?.({ preventScroll: true });
    };
  }, []);
  return ref;
}

export function Modal({ label, eyebrow, title, onClose, children }:
  { label?: string; eyebrow: ReactNode; title: ReactNode; onClose: () => void; children: ReactNode }) {
  const ref = useLayer(onClose);
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label={label ?? String(title)}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="box" ref={ref as React.RefObject<HTMLDivElement>} tabIndex={-1}>
        <button className="x" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        <div className="eyebrow">{eyebrow}</div>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function Drawer({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  const ref = useLayer(onClose);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}
        ref={ref as React.RefObject<HTMLElement>}>
        <button className="x" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        {children}
      </aside>
    </>
  );
}

/* ---------------------------------------------------------------- small pieces */

export function Banner({ icon = 'alert', ok, children }:
  { icon?: Parameters<typeof Icon>[0]['name']; ok?: boolean; children: ReactNode }) {
  return <div className={`banner${ok ? ' ok' : ''}`}><Icon name={icon} />{children}</div>;
}

export const Tags = ({ tags }: { tags?: string[] }) =>
  tags && tags.length ? <span className="tags">{tags.map(t => <span key={t} className="tag">{t}</span>)}</span> : null;

/** A tick box that can also show "some of these" (indeterminate), which has no HTML attribute. */
export function Tick({ checked, some, label, onChange }:
  { checked: boolean; some?: boolean; label: string; onChange: () => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = !!some && !checked; }, [some, checked]);
  return <input ref={ref} type="checkbox" aria-label={label} checked={checked} onChange={onChange}
    onClick={e => e.stopPropagation()} />;
}
