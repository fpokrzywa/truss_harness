'use client';

import { useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';
import {
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  clampSidebarWidth,
  readSidebarLayout,
  saveSidebarLayout,
  type SidebarLayout,
} from '@/lib/sidebar-layout';

const KEY_STEP = 16;

/**
 * Column around the navigation: scrolls on its own beside the right panel, collapses to a rail
 * and resizes by its edge. Rendered only after the session loads, so reading storage is safe here.
 */
export function SidebarFrame({
  children,
}: {
  children: (rail: { collapsed: boolean; expand: () => void }) => ReactNode;
}) {
  const [layout, setLayout] = useState(readSidebarLayout);
  const frame = useRef<HTMLElement>(null);
  const { width, collapsed } = layout;

  function commit(next: SidebarLayout) {
    setLayout(next);
    saveSidebarLayout(next);
  }

  function onHandleKey(e: KeyboardEvent) {
    const target: Partial<Record<string, number>> = {
      ArrowLeft: width - KEY_STEP,
      ArrowRight: width + KEY_STEP,
      Home: SIDEBAR_MIN_WIDTH,
      End: SIDEBAR_MAX_WIDTH,
    };
    const next = target[e.key];
    if (next === undefined) return;
    e.preventDefault();
    commit({ ...layout, width: clampSidebarWidth(next) });
  }

  return (
    <aside
      ref={frame}
      style={{ '--sidebar-width': `${width}px` } as CSSProperties}
      className={`relative z-20 w-full shrink-0 border-b border-slate-200 bg-slate-100 md:border-r md:border-b-0 ${
        collapsed ? 'md:w-12' : 'md:w-[min(var(--sidebar-width),50vw)]'
      }`}
    >
      <div
        className={`max-h-56 overflow-y-auto md:h-full md:max-h-none ${
          collapsed ? 'md:overflow-visible' : ''
        }`}
      >
        <div className="flex justify-end p-1">
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-label={collapsed ? 'Open navigation' : 'Close navigation'}
            title={collapsed ? 'Open navigation' : 'Close navigation'}
            onClick={() => commit({ ...layout, collapsed: !collapsed })}
            className="flex h-10 w-10 items-center justify-center rounded-md text-slate-900 hover:bg-slate-50"
          >
            <svg aria-hidden="true" viewBox="0 0 16 16" width="16" height="16">
              <path
                d={collapsed ? 'M3 3l5 5-5 5M8 3l5 5-5 5' : 'M13 3L8 8l5 5M8 3L3 8l5 5'}
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
              />
            </svg>
          </button>
        </div>
        {children({ collapsed, expand: () => commit({ ...layout, collapsed: false }) })}
      </div>

      {!collapsed && (
        <div
          role="separator"
          tabIndex={0}
          aria-orientation="vertical"
          aria-label="Resize navigation"
          aria-valuenow={width}
          aria-valuemin={SIDEBAR_MIN_WIDTH}
          aria-valuemax={SIDEBAR_MAX_WIDTH}
          title="Drag to resize. Double-click to reset."
          onKeyDown={onHandleKey}
          onDoubleClick={() => commit({ ...layout, width: SIDEBAR_DEFAULT_WIDTH })}
          onPointerDown={(e) => {
            e.preventDefault();
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (!e.currentTarget.hasPointerCapture(e.pointerId) || !frame.current) return;
            const left = frame.current.getBoundingClientRect().left;
            setLayout((l) => ({ ...l, width: clampSidebarWidth(e.clientX - left) }));
          }}
          onPointerUp={() => saveSidebarLayout(layout)}
          className="group absolute inset-y-0 -right-1.5 z-10 hidden w-3 cursor-col-resize touch-none md:block"
        >
          <span
            aria-hidden="true"
            className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 group-hover:bg-shi-pink group-focus-visible:bg-shi-pink group-active:bg-shi-pink"
          />
        </div>
      )}
    </aside>
  );
}
