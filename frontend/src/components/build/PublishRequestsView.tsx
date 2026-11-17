'use client';

import { useState } from 'react';
import { endpoints } from '@/lib/api/endpoints';
import type { PublishRequest } from '@/lib/api/build-types';
import { ApiError } from '@/lib/api/errors';
import { usePublishRequests, useRefreshBuild } from '@/lib/build-hooks';
import { useCan, useSession } from '@/lib/hooks';
import {
  ActionError,
  Badge,
  EmptyState,
  formatDate,
  Modal,
  ModalActions,
  PageHeader,
  QueryGate,
  TextField,
  useAction,
} from '../admin/ui';
import { NoAccess } from '../NoAccess';
import { useOrgParam } from '../security/useOrg';

export function PublishRequestsView() {
  const org = useOrgParam();
  const { can, loaded } = useCan(org);
  const session = useSession();
  const requests = usePublishRequests(org);
  const [deciding, setDeciding] = useState<{
    request: PublishRequest;
    decision: 'approve' | 'reject';
  } | null>(null);
  const me = session.data?.user.id;

  if (loaded && !can('approval:decide')) return <NoAccess />;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Publish Requests"
        description="Requests to publish versions that need approval. You cannot decide your own requests."
      />
      <QueryGate query={requests} noun="publish requests">
        {(data) => (
          <section aria-label="Publish requests" className="card p-0">
            {data.items.length === 0 ? (
              <EmptyState>No publish requests.</EmptyState>
            ) : (
              <table className="w-full">
                <caption className="sr-only">Publish requests</caption>
                <thead className="border-b border-slate-200">
                  <tr>
                    {[
                      'Artifact',
                      'Version',
                      'Requested by',
                      'Change note',
                      'Status',
                      'Actions',
                    ].map((h) => (
                      <th key={h} scope="col" className="th">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.items.map((r) => {
                    const own = !!me && r.requested_by.id === me;
                    return (
                      <tr key={r.id}>
                        <td className="td font-medium">
                          {r.artifact.name}
                          <div className="text-xs text-slate-600">{r.artifact.kind}</div>
                        </td>
                        <td className="td">v{r.version_no}</td>
                        <td className="td">
                          {r.requested_by.display}
                          <div className="text-xs text-slate-600">{formatDate(r.requested_at)}</div>
                        </td>
                        <td className="td">{r.change_note ?? '-'}</td>
                        <td className="td">
                          <Badge
                            tone={
                              r.status === 'approved'
                                ? 'good'
                                : r.status === 'rejected'
                                  ? 'bad'
                                  : 'warn'
                            }
                          >
                            {r.status}
                          </Badge>
                        </td>
                        <td className="td">
                          {r.status === 'pending' &&
                            (own ? (
                              <span className="text-xs text-slate-600">Your own request</span>
                            ) : (
                              <div className="flex gap-1">
                                <button
                                  type="button"
                                  className="btn"
                                  aria-label={`Approve ${r.artifact.name}`}
                                  onClick={() => setDeciding({ request: r, decision: 'approve' })}
                                >
                                  Approve
                                </button>
                                <button
                                  type="button"
                                  className="btn"
                                  aria-label={`Reject ${r.artifact.name}`}
                                  onClick={() => setDeciding({ request: r, decision: 'reject' })}
                                >
                                  Reject
                                </button>
                              </div>
                            ))}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        )}
      </QueryGate>
      {deciding && <DecideDialog org={org} {...deciding} onClose={() => setDeciding(null)} />}
    </div>
  );
}

function DecideDialog({
  org,
  request,
  decision,
  onClose,
}: {
  org: string;
  request: PublishRequest;
  decision: 'approve' | 'reject';
  onClose: () => void;
}) {
  const [comment, setComment] = useState('');
  const refresh = useRefreshBuild(org);
  const { busy, error, run } = useAction();
  const forbidden = error instanceof ApiError && error.status === 403;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const r = await run(() =>
      endpoints.decidePublishRequest(org, request.id, decision, comment.trim()),
    );
    if (r.ok) {
      await refresh();
      onClose();
    }
  }

  return (
    <Modal
      title={`${decision === 'approve' ? 'Approve' : 'Reject'} publish request`}
      onClose={onClose}
      busy={busy}
    >
      <form onSubmit={submit} className="space-y-3">
        <p className="text-sm">
          {request.artifact.name} v{request.version_no}
        </p>
        <TextField label="Comment (optional)" value={comment} onChange={setComment} multiline />
        {forbidden ? (
          <p role="alert" className="text-sm text-red-800">
            The server refused this decision (403). The approver must be different from the
            requester, and you need the approval:decide permission.
          </p>
        ) : (
          <ActionError error={error} title="Could not record decision" />
        )}
        <ModalActions
          onCancel={onClose}
          submitLabel={decision === 'approve' ? 'Approve' : 'Reject'}
          busy={busy}
        />
      </form>
    </Modal>
  );
}
