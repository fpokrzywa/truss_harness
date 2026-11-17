'use client';

import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { ToneGlyph } from './ToneGlyph';

export interface TabItem {
  id: string;
  label: string;
  /** Something inside needs attention: shown on the tab so a closed panel cannot hide it. */
  alert?: boolean;
  content: ReactNode;
}

/** WAI-ARIA tabs. Panels stay mounted, so reading one tab does not restart another. */
export function Tabs({ label, tabs }: { label: string; tabs: readonly TabItem[] }) {
  const [selected, setSelected] = useState(tabs[0]?.id);
  const base = useId();
  const buttons = useRef<Record<string, HTMLButtonElement | null>>({});

  function onKeyDown(e: KeyboardEvent, index: number) {
    const last = tabs.length - 1;
    const next: Partial<Record<string, number>> = {
      ArrowRight: index === last ? 0 : index + 1,
      ArrowLeft: index === 0 ? last : index - 1,
      Home: 0,
      End: last,
    };
    const target = tabs[next[e.key] ?? -1];
    if (!target) return;
    e.preventDefault();
    setSelected(target.id);
    buttons.current[target.id]?.focus();
  }

  return (
    <div className="min-w-0">
      <div
        role="tablist"
        aria-label={label}
        className="flex flex-wrap gap-1 border-b-[3px] border-shi-pink"
      >
        {tabs.map((tab, index) => {
          const active = tab.id === selected;
          return (
            <button
              key={tab.id}
              ref={(el) => {
                buttons.current[tab.id] = el;
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${tab.id}`}
              aria-selected={active}
              aria-controls={`${base}-panel-${tab.id}`}
              tabIndex={active ? 0 : -1}
              onClick={() => setSelected(tab.id)}
              onKeyDown={(e) => onKeyDown(e, index)}
              className={`inline-flex min-h-11 items-center gap-2 px-3 text-sm font-medium ${
                active ? 'bg-shi-mid-deep font-bold text-shi-ink' : 'text-shi-ink hover:bg-shi-grey'
              }`}
            >
              {tab.label}
              {tab.alert && (
                <>
                  <ToneGlyph tone="warn" size={12} />
                  <span className="sr-only">, needs attention</span>
                </>
              )}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={`${base}-panel-${tab.id}`}
          aria-labelledby={`${base}-tab-${tab.id}`}
          hidden={tab.id !== selected}
          className="pt-4"
        >
          {tab.content}
        </div>
      ))}
    </div>
  );
}
