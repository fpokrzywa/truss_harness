import type { Approval, Run } from '@/lib/api/operate-types';
import { ALL_PERMS, stubApi } from './security';
import type { Handler } from './utils';

export const OPERATE_PERMS = [
  ...ALL_PERMS,
  'run:read',
  'run:cancel',
  'agent:read',
  'agent:execute',
  'approval:read',
  'approval:decide',
];

export function run(over: Partial<Run> = {}): Run {
  return {
    id: 'run-1',
    kind: 'agent',
    status: 'running',
    tenant_id: 'org-1',
    workspace: { id: 'ws-1', slug: 'eng' },
    project: { id: 'pj-1', slug: 'api' },
    initiated_by: { type: 'user', id: 'u-other', display: 'Olive' },
    agent_version_id: 'av-1',
    prompt_version_id: 'pv-1',
    trace_id: 'trace-abc',
    correlation_id: 'corr-xyz',
    created_at: '2026-11-03T10:00:00Z',
    attempt_count: 1,
    outcome_unknown: false,
    usage: { input_tokens: 10, output_tokens: 5, cached_tokens: 0, model_calls: 1, tool_calls: 0 },
    ...over,
  };
}

export function approval(over: Partial<Approval> = {}): Approval {
  return {
    id: 'ap-1',
    run_id: 'run-1',
    step_id: 's-1',
    reason: 'tool_side_effect',
    tool: { key: 'crm.update', version: 3, side_effect_class: 'write_non_idempotent' },
    arguments: { customer_id: 'c-9', patch: { tier: 'gold' } },
    requested_by: { type: 'user', id: 'u-other', display: 'Olive' },
    requested_at: '2026-11-03T10:00:00Z',
    expires_at: '2099-01-01T00:00:00Z',
    status: 'pending',
    run_initiated_by: { type: 'user', id: 'u-other', display: 'Olive' },
    ...over,
  };
}

export const stubOperate = (routes: Record<string, Handler> = {}, perms = OPERATE_PERMS) =>
  stubApi(routes, perms);
