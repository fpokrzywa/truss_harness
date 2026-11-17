import type { AgentSpec, SideEffectClass } from './api/build-types';

export const LATEST_PREFIX = 'latest:';

export type VersionRefValue =
  { mode: 'latest'; artifactId: string } | { mode: 'pinned'; versionId: string };

/** Draft agent specs reference an artifact as `latest:{artifact_id}`; publish pins it to a version id. */
export const latestRef = (artifactId: string) => `${LATEST_PREFIX}${artifactId}`;

export function parseRef(value: string): VersionRefValue {
  return value.startsWith(LATEST_PREFIX)
    ? { mode: 'latest', artifactId: value.slice(LATEST_PREFIX.length) }
    : { mode: 'pinned', versionId: value };
}

export const REQUIRED_APPROVAL: SideEffectClass = 'destructive';
export const DEFAULT_APPROVALS: SideEffectClass[] = ['write_non_idempotent', 'destructive'];
export const APPROVABLE: SideEffectClass[] = [
  'write_idempotent',
  'write_non_idempotent',
  'destructive',
];

/** Destructive approval can never be removed. */
export function withDestructive(list: SideEffectClass[]): SideEffectClass[] {
  return list.includes(REQUIRED_APPROVAL) ? list : [...list, REQUIRED_APPROVAL];
}

export function defaultAgentSpec(): AgentSpec {
  return {
    prompt_version_id: '',
    model: { model_id: '', fallback_model_ids: [] },
    tool_version_ids: [],
    skill_version_ids: [],
    memory: { mode: 'none', max_turns: 10 },
    limits: { max_iterations: 10, max_tool_calls: 20, max_tokens: 4000, deadline_s: 120 },
    execution_permissions: {
      allowed_principal_types: ['user', 'service_account'],
      required_permission: 'agent:execute',
    },
    approval_rules: { require_approval_for: [...DEFAULT_APPROVALS] },
    variables_map: {},
  };
}
