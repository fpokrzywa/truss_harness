'use client';

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';

interface Item {
  id: string;
  title: string;
  el: Element;
}

export interface SectionHost {
  register: (item: Item) => () => void;
  activeId: string | null;
  count: number;
}

/** Null outside SectionTabs, and inside a section: there, sections stack as plain cards. */
export const SectionHostContext = createContext<SectionHost | null>(null);

export const useSectionHost = () => useContext(SectionHostContext);

/** True once the enclosing section has been on screen. Always true outside SectionTabs. */
export const SectionShownContext = createContext(true);

/** For content that measures itself, like a graph canvas: wait until it has a real size to measure. */
export const useSectionFirstShown = () => useContext(SectionShownContext);

export const sectionTabId = (id: string) => `section-tab-${id}`;
export const sectionPanelId = (id: string) => `section-panel-${id}`;

/** Document order, so a section that appears later still takes its place in the row. */
const inPageOrder = (items: Item[]) =>
  [...items].sort((a, b) =>
    a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1,
  );

/**
 * Shows the `Section`s inside it one at a time under a tab row, styled like the run detail tabs.
 * Sections announce themselves, so an editor needs no changes. Hidden panels stay mounted, which
 * keeps half-typed text and the field errors that block saving.
 */
export function SectionTabs({ label, children }: { label: string; children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  const register = useCallback((item: Item) => {
    setItems((cur) => inPageOrder([...cur.filter((i) => i.id !== item.id), item]));
    return () => setItems((cur) => cur.filter((i) => i.id !== item.id));
  }, []);

  const activeId = items.some((i) => i.id === selected) ? selected : (items[0]?.id ?? null);
  const host = useMemo(
    () => ({ register, activeId, count: items.length }),
    [register, activeId, items.length],
  );

  function onKeyDown(e: KeyboardEvent, index: number) {
    const last = items.length - 1;
    const next: Partial<Record<string, number>> = {
      ArrowRight: index === last ? 0 : index + 1,
      ArrowLeft: index === 0 ? last : index - 1,
      Home: 0,
      End: last,
    };
    const target = items[next[e.key] ?? -1];
    if (!target) return;
    e.preventDefault();
    setSelected(target.id);
    buttons.current[target.id]?.focus();
  }

  return (
    <SectionHostContext.Provider value={host}>
      {items.length > 1 && (
        <div
          role="tablist"
          aria-label={label}
          className="mb-4 flex flex-wrap gap-1 border-b border-slate-200"
        >
          {items.map((item, index) => {
            const active = item.id === activeId;
            return (
              <button
                key={item.id}
                ref={(el) => {
                  buttons.current[item.id] = el;
                }}
                type="button"
                role="tab"
                id={sectionTabId(item.id)}
                aria-selected={active}
                aria-controls={sectionPanelId(item.id)}
                tabIndex={active ? 0 : -1}
                onClick={() => setSelected(item.id)}
                onKeyDown={(e) => onKeyDown(e, index)}
                className={`px-3 py-2 text-sm font-medium ${
                  active ? 'border-b-2 border-blue-800 text-blue-900' : 'text-slate-600'
                }`}
              >
                {item.title}
              </button>
            );
          })}
        </div>
      )}
      {children}
    </SectionHostContext.Provider>
  );
}
