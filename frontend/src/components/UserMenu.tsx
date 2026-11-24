'use client';

import { useEffect, useId, useRef, useState } from 'react';
import type { User } from '@/lib/api/types';
import { SignOutButton } from './SignOutButton';

export function UserMenu({ user }: { user: User }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    function onPointer(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onPointer);
    };
  }, [open]);

  return (
    <div ref={wrapRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        className="btn"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="sr-only">User menu: </span>
        {user.display_name || user.email}
      </button>
      {open && (
        <div
          id={panelId}
          className="absolute right-0 z-20 mt-1 w-72 rounded-md border border-slate-200 bg-white p-3 shadow-lg"
        >
          <p className="text-sm font-medium text-slate-900">{user.display_name}</p>
          <p className="text-xs break-all text-slate-600">{user.email}</p>
          {user.is_platform_admin && (
            <p className="mt-1 text-xs text-slate-600">Platform administrator</p>
          )}
          <SignOutButton className="btn mt-3 w-full" />
        </div>
      )}
    </div>
  );
}
