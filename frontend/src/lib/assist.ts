import {
  CONTEXT_KINDS,
  type AssistContext,
  type AssistRequestStatus,
  type CatalogEntryOut,
  type ContextKind,
  type ProposalKind,
  type ProposalStatus,
  type RouteMethod,
} from './api/assist-types';
import { deliverHref, evaluationsHref, releaseHref, solutionHref } from './scope';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The context of `?context=<kind>:<id>`; anything but a known kind and a UUID is no context. */
export function parseContext(value: string | null): AssistContext | null {
  const [kind, id, ...rest] = (value ?? '').split(':');
  if (id === undefined || rest.length > 0 || !UUID.test(id)) return null;
  const known = CONTEXT_KINDS.find((k) => k === kind);
  return known ? { kind: known, id } : null;
}

export const CONTEXT_LABEL: Record<ContextKind, string> = {
  solution: 'solution',
  release: 'release',
  target: 'target',
  plan: 'plan',
  deployment: 'deployment',
  evaluation_run: 'evaluation run',
};

/** The context object's own screen; null where there is none or its project is not known. */
export function contextHref(
  org: string,
  { kind, id }: AssistContext,
  project: { ws: string; proj: string } | null,
): string | null {
  switch (kind) {
    case 'solution':
      return project && solutionHref(org, id, project.ws, project.proj);
    case 'release':
      return releaseHref(org, id);
    case 'plan':
      return deliverHref(org, `plans/${encodeURIComponent(id)}`);
    case 'deployment':
      return deliverHref(org, `deployments/${encodeURIComponent(id)}`);
    case 'evaluation_run':
      return evaluationsHref(org, encodeURIComponent(id));
    case 'target':
      return null;
  }
}

const REASONS: Record<string, string> = {
  route_ambiguous: 'More than one specialist fits this request.',
  route_unclear: 'SETH could not tell which specialist fits this request.',
  specialist_disabled:
    'The specialist for this request is turned off, for the organization or in this project.',
  specialists_not_installed: 'The specialist for this request is not installed in this project.',
  specialist_not_permitted:
    'The person asking does not hold every permission the specialist needs.',
  specialist_modified:
    'The installed specialist differs from its catalog entry and must be installed again.',
  router_model_missing: 'Model routing is on, but no router model is set.',
  access_revoked: 'The person asking lost access before the request was routed.',
  model_call_unknown: 'The router model call was interrupted, so its outcome is unknown.',
};

/** One plain sentence for a request's `status_reason`; an unknown code is shown as it is. */
export const reasonText = (code: string): string =>
  Object.hasOwn(REASONS, code) ? (REASONS[code] as string) : code;

type Tone = 'neutral' | 'good' | 'warn' | 'bad';

export const REQUEST_STATUS: Record<AssistRequestStatus, { label: string; tone: Tone }> = {
  queued: { label: 'Queued', tone: 'neutral' },
  classifying: { label: 'Classifying', tone: 'warn' },
  handing_over: { label: 'Handing over', tone: 'warn' },
  routed: { label: 'Routed', tone: 'good' },
  refused: { label: 'Refused', tone: 'bad' },
  failed: { label: 'Failed', tone: 'bad' },
};

export const PROPOSAL_STATUS: Record<ProposalStatus, { label: string; tone: Tone }> = {
  open: { label: 'Open', tone: 'warn' },
  accepted: { label: 'Accepted', tone: 'good' },
  failed: { label: 'Failed', tone: 'bad' },
  dismissed: { label: 'Dismissed', tone: 'neutral' },
};

export const METHOD_LABEL: Record<RouteMethod, string> = {
  explicit: 'Chosen by the person asking',
  rule: 'Routing rule',
  model: 'Router model',
};

export const PROPOSAL_KIND: Record<ProposalKind, string> = {
  appgen_request: 'App request',
  evaluation_run: 'Evaluation run',
  delivery_plan: 'Delivery plan',
};

/** A specialist's catalog title, or its key while the catalog does not name it. */
export const specialistTitle = (entries: CatalogEntryOut[] | undefined, key: string): string =>
  entries?.find((e) => e.key === key)?.title ?? key;
