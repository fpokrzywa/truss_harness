import { describe, expect, it } from 'vitest';
import { defaultAgentSpec, latestRef, parseRef, withDestructive } from './agent-spec';

describe('agent version references', () => {
  it('encodes latest as latest:{artifact_id} and pins as the bare version id', () => {
    expect(latestRef('a-9')).toBe('latest:a-9');
    expect(parseRef('latest:a-9')).toEqual({ mode: 'latest', artifactId: 'a-9' });
    expect(parseRef('v-77')).toEqual({ mode: 'pinned', versionId: 'v-77' });
  });

  it('defaults approval to write_non_idempotent + destructive', () => {
    expect(defaultAgentSpec().approval_rules.require_approval_for).toEqual([
      'write_non_idempotent',
      'destructive',
    ]);
  });

  it('always keeps destructive approval', () => {
    expect(withDestructive(['write_idempotent'])).toEqual(['write_idempotent', 'destructive']);
    expect(withDestructive(['destructive'])).toEqual(['destructive']);
  });
});
