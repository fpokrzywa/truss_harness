'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useEffect, useId, useRef, useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { User } from '@/lib/api/types';
import { ErrorMessage } from './ErrorMessage';

export function UserMenu({ user }: { user: User }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const router = useRouter();
  const queryClient = useQueryClient();

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

  async function logout() {
    setBusy(true);
    setError(null);
    try {
      await endpoints.logout();
      queryClient.clear();
      router.replace('/login');
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  }

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
          {error !== null && (
            <div className="mt-2">
              <ErrorMessage error={error} title="Could not sign out" />
            </div>
          )}
          <button
            type="button"
            className="btn mt-3 w-full"
            onClick={() => void logout()}
            disabled={busy}
          >
            {busy ? 'Signing out...' : 'Sign out'}
          </button>
        </div>
      )}
    </div>
  );
}
