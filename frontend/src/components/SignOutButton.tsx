'use client';

import { useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import { ErrorMessage } from './ErrorMessage';

/** Signs out, clears cached queries and goes to `/login`; shows the error if it fails. */
export function SignOutButton({ className = 'btn' }: { className?: string }) {
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  const queryClient = useQueryClient();

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
    <>
      {error !== null && (
        <div className="mt-2">
          <ErrorMessage error={error} title="Could not sign out" />
        </div>
      )}
      <button type="button" className={className} onClick={() => void logout()} disabled={busy}>
        {busy ? 'Signing out...' : 'Sign out'}
      </button>
    </>
  );
}
