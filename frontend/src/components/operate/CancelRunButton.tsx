'use client';

import { useQueryClient } from '@tanstack/react-query';
import { endpoints } from '@/lib/api/endpoints';
import type { Run } from '@/lib/api/operate-types';
import { useCan, useSession } from '@/lib/hooks';
import { canCancel } from '@/lib/run-utils';
import { ActionError, useAction } from '../admin/ui';

/** Cancel is offered only for non-terminal runs, to run:cancel holders or the initiator. */
export function CancelRunButton({ org, run }: { org: string; run: Run }) {
  const { can } = useCan(org);
  const session = useSession();
  const qc = useQueryClient();
  const { busy, error, run: act } = useAction();
  const allowed = can('run:cancel') || run.initiated_by.id === session.data?.user.id;
  if (!canCancel(run.status) || !allowed) return null;

  async function cancel() {
    const r = await act(() => endpoints.cancelRun(org, run.id));
    if (r.ok) await qc.invalidateQueries({ queryKey: ['operate', org] });
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        className="btn btn-danger"
        disabled={busy}
        onClick={() => void cancel()}
      >
        {busy ? 'Cancelling...' : 'Cancel run'}
      </button>
      <ActionError error={error} title="Could not cancel the run" />
    </div>
  );
}
