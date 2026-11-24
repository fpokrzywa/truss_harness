'use client';

import Link from 'next/link';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import { useState, type FormEvent } from 'react';
import {
  ASSIST_REQUEST_STATUSES,
  type AssistCatalogOut,
  type AssistContext,
  type AssistRequestOut,
  type AssistRequestStatus,
  type CatalogEntryOut,
} from '@/lib/api/assist-types';
import { endpoints } from '@/lib/api/endpoints';
import {
  CONTEXT_LABEL,
  METHOD_LABEL,
  parseContext,
  reasonText,
  REQUEST_STATUS,
  specialistTitle,
} from '@/lib/assist';
import {
  useAssistCatalog,
  useAssistRequests,
  useRefreshAssist,
  useSpecialists,
} from '@/lib/assist-hooks';
import { useCan, usePermissions } from '@/lib/hooks';
import { assistHref, assistRequestHref } from '@/lib/scope';
import {
  ActionError,
  EmptyState,
  formatDate,
  PageHeader,
  QueryGate,
  SelectField,
  TextField,
  useAction,
} from '../admin/ui';
import { NoAccess } from '../NoAccess';
import { useOrgParam } from '../security/useOrg';
import { linkClass, RequestStatusBadge, SettingsHint, type ProjectScope } from './parts';

const MAX_TEXT = 4000;

/** The project's Ask SETH screen: a new request and the project's requests. Plain React text only. */
export function AskSethView() {
  const org = useOrgParam();
  const params = useParams<{ ws: string; proj: string }>();
  const scope = {
    org,
    ws: decodeURIComponent(params.ws),
    proj: decodeURIComponent(params.proj),
  };
  const permissions = usePermissions(org, scope.ws, scope.proj);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Ask SETH"
        description="Ask a question or describe what you need: SETH hands it to one of this project's specialists, which answers and can propose work for a person to accept."
      />
      <QueryGate query={permissions} noun="permissions">
        {(perms) => {
          const canAsk = perms.has('assist:request:write');
          if (!canAsk && !perms.has('assist:request:read')) return <NoAccess />;
          return (
            <>
              {canAsk ? (
                <AskPanel scope={scope} canInstall={perms.has('assist:install')} />
              ) : (
                <p className="text-sm text-slate-700">
                  You can view this project&apos;s requests but not ask.
                </p>
              )}
              <RequestsList scope={scope} />
            </>
          );
        }}
      </QueryGate>
    </div>
  );
}

/** The refused answer on screen, with the context value it was asked about. */
interface Refusal {
  answer: AssistRequestOut;
  contextValue: string | null;
}

function AskPanel({ scope, canInstall }: { scope: ProjectScope; canInstall: boolean }) {
  const { org, ws, proj } = scope;
  const router = useRouter();
  const { can, loaded } = useCan(org);
  const catalog = useAssistCatalog(org);
  const specialists = useSpecialists(org, ws, proj);
  const refresh = useRefreshAssist(org);
  const action = useAction();
  // `?context=`, unless the user removed that one: the URL only follows after the navigation.
  const [removed, setRemoved] = useState<string | null>(null);
  const param = useSearchParams().get('context');
  const contextValue = param === removed ? null : param;
  const context = parseContext(contextValue);
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [specialist, setSpecialist] = useState('');
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  // The request's own screen is opening: nothing more can be sent from this one.
  const [leaving, setLeaving] = useState(false);
  const busy = action.busy || leaving;

  async function send(key: string) {
    const trimmed = text.trim();
    if (trimmed === '' || [...trimmed].length > MAX_TEXT) {
      setError(`The request must be 1 to ${MAX_TEXT} characters.`);
      return;
    }
    setError(null);
    const res = await action.run(() =>
      endpoints.createAssistRequest(org, ws, proj, {
        text: trimmed,
        ...(key ? { specialist: key } : {}),
        ...(context ? { context } : {}),
      }),
    );
    if (!res.ok) return;
    void refresh();
    if (res.value.status === 'refused') {
      setRefusal({ answer: res.value, contextValue });
      return;
    }
    setLeaving(true);
    router.push(assistRequestHref(org, res.value.id));
  }

  function removeContext() {
    setRemoved(param);
    setRefusal(null);
    action.reset();
    router.replace(assistHref(org, ws, proj));
  }

  function form(data: AssistCatalogOut, available: ReadonlySet<string>) {
    return (
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void send(specialist);
        }}
        className="card max-w-3xl space-y-4"
        aria-label="New request"
        noValidate
      >
        {context && <ContextLine context={context} disabled={busy} onRemove={removeContext} />}
        <TextField
          label="Request"
          value={text}
          onChange={(v) => {
            setText(v);
            setRefusal(null);
            action.reset();
          }}
          multiline
          readOnly={busy}
          help={`What you want to know or need, in up to ${MAX_TEXT} characters.`}
          error={error}
        />
        <SelectField
          label="Specialist (optional)"
          value={specialist}
          disabled={busy}
          onChange={setSpecialist}
          options={[
            { value: '', label: 'Let SETH choose' },
            ...data.entries.map((e) => ({
              value: e.key,
              label: available.has(e.key) ? e.title : `${e.title} (not available in this project)`,
            })),
          ]}
        />
        <ActionError error={action.error} title="Could not send the request" />
        <div className="flex justify-end">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Sending...' : 'Send'}
          </button>
        </div>
        {refusal !== null && refusal.contextValue === contextValue && (
          <RefusalPanel
            key={refusal.answer.id}
            scope={scope}
            answer={refusal.answer}
            entries={data.entries}
            canInstall={canInstall}
            busy={busy}
            onPick={(key) => void send(key)}
          />
        )}
      </form>
    );
  }

  const settings = loaded && (
    <SettingsHint {...scope} allowed={canInstall || can('assist:admin')} />
  );
  return (
    <QueryGate query={catalog} noun="specialists">
      {(data) =>
        data.assist_enabled ? (
          <QueryGate query={specialists} noun="specialists">
            {(rows) => {
              const available = new Set(rows.filter((s) => s.available).map((s) => s.key));
              return (
                <div className="space-y-4">
                  {available.size === 0 && (
                    <p className="card max-w-3xl text-sm text-slate-700">
                      No specialist is available in this project. {settings}
                    </p>
                  )}
                  {form(data, available)}
                </div>
              );
            }}
          </QueryGate>
        ) : (
          <p className="card max-w-3xl text-sm text-slate-700">
            Ask SETH is turned off for this organization, or pinned to an older catalog version.{' '}
            {settings}
          </p>
        )
      }
    </QueryGate>
  );
}

function ContextLine({
  context,
  disabled,
  onRemove,
}: {
  context: AssistContext;
  disabled: boolean;
  onRemove: () => void;
}) {
  const label = CONTEXT_LABEL[context.kind];
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm text-slate-700">
      <span>
        About: {label} <span className="font-mono text-xs break-all">{context.id}</span>
      </span>
      <button type="button" className="btn" disabled={disabled} onClick={onRemove}>
        Remove <span className="sr-only">the {label}</span>
      </button>
    </p>
  );
}

/** Why a request was refused, and what can be done about it from here. */
function RefusalPanel({
  scope,
  answer,
  entries,
  canInstall,
  busy,
  onPick,
}: {
  scope: ProjectScope;
  answer: AssistRequestOut;
  entries: CatalogEntryOut[];
  canInstall: boolean;
  busy: boolean;
  onPick: (key: string) => void;
}) {
  const code = answer.status_reason;
  const picks =
    code === 'route_ambiguous'
      ? answer.candidates
      : code === 'route_unclear'
        ? entries.map((e) => e.key)
        : [];
  const missing = answer.missing_permissions ?? [];
  return (
    <div
      role="alert"
      className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
    >
      <p className="font-semibold">The request was refused</p>
      {code !== null && <p>{reasonText(code)}</p>}
      {code === 'specialist_not_permitted' && missing.length > 0 && (
        <>
          <p>Missing permissions:</p>
          <ul className="list-disc pl-5 font-mono text-xs">
            {missing.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </>
      )}
      {(code === 'specialists_not_installed' || code === 'specialist_modified') && (
        <p>
          <SettingsHint {...scope} allowed={canInstall} />
        </p>
      )}
      {picks.length > 0 && (
        <>
          <p>Send the same request to:</p>
          <div className="flex flex-wrap gap-2">
            {picks.map((key) => (
              <button
                key={key}
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => onPick(key)}
              >
                {specialistTitle(entries, key)}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

const STATUS_OPTIONS = [
  { value: '', label: 'All' },
  ...ASSIST_REQUEST_STATUSES.map((s) => ({ value: s, label: REQUEST_STATUS[s].label })),
];

function RequestsList({ scope }: { scope: ProjectScope }) {
  const { org, ws, proj } = scope;
  const [status, setStatus] = useState<AssistRequestStatus | ''>('');
  const requests = useAssistRequests(org, ws, proj, status);
  const catalog = useAssistCatalog(org);

  return (
    <section aria-label="Requests" className="space-y-4">
      <h2 className="text-lg font-semibold text-slate-900">Requests</h2>
      <div className="max-w-xs">
        <SelectField
          label="Status"
          value={status}
          onChange={(v) => setStatus(v as AssistRequestStatus | '')}
          options={STATUS_OPTIONS}
        />
      </div>
      <QueryGate query={requests} noun="requests">
        {(data) => {
          const items = data.pages.flatMap((p) => p.items);
          return (
            <div className="card overflow-x-auto p-0">
              {items.length === 0 ? (
                <EmptyState>
                  {status === ''
                    ? 'No requests in this project yet.'
                    : 'No requests with this status in this project.'}
                </EmptyState>
              ) : (
                <table className="w-full">
                  <caption className="sr-only">Requests</caption>
                  <thead className="border-b border-slate-200">
                    <tr>
                      {['Status', 'Reason', 'Specialist', 'Route', 'Created'].map((h) => (
                        <th key={h} scope="col" className="th">
                          {h}
                        </th>
                      ))}
                      <th scope="col" className="th">
                        <span className="sr-only">Request</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {items.map((r) => (
                      <tr key={r.id}>
                        <td className="td">
                          <RequestStatusBadge status={r.status} />
                        </td>
                        <td className="td">
                          {r.status_reason === null ? '-' : reasonText(r.status_reason)}
                        </td>
                        <td className="td">
                          {r.specialist_key === null
                            ? '-'
                            : specialistTitle(catalog.data?.entries, r.specialist_key)}
                        </td>
                        <td className="td">
                          {r.route_method === null ? '-' : METHOD_LABEL[r.route_method]}
                        </td>
                        <td className="td">{formatDate(r.created_at)}</td>
                        <td className="td">
                          <Link className={linkClass} href={assistRequestHref(org, r.id)}>
                            Open{' '}
                            <span className="sr-only">
                              request created {formatDate(r.created_at)}
                            </span>
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {requests.hasNextPage && (
                <div className="border-t border-slate-200 p-3">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void requests.fetchNextPage()}
                    disabled={requests.isFetchingNextPage}
                  >
                    {requests.isFetchingNextPage ? 'Loading...' : 'Load more'}
                  </button>
                </div>
              )}
            </div>
          );
        }}
      </QueryGate>
    </section>
  );
}
