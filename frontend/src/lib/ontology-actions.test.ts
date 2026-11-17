import { describe, expect, it } from 'vitest';
import type { ObjectType, OntAction, OntologySpec } from './api/ontology-types';
import { checkAction, emptyOntologySpec, ontologyBlockers, toolParams } from './ontology-spec';

const customer: ObjectType = {
  key: 'crm.customer',
  label: 'Customer',
  namespace: 'crm',
  properties: [
    { key: 'customer_id', type: 'integer', nullable: false, classification: 'internal' },
    { key: 'name', type: 'string', nullable: true, classification: 'public' },
  ],
  primary_key: ['customer_id'],
};
const spec = (): OntologySpec => ({
  ...emptyOntologySpec(),
  namespaces: [{ key: 'crm', label: 'CRM' }],
  object_types: [customer],
});
const action = {
  key: 'crm.escalate',
  object_type: 'crm.customer',
  tool_version_id: 'tv-1',
  param_mapping: { id: '$object.customer_id', note: 'literal' },
};

describe('action checks', () => {
  it('accepts object references and literals', () => {
    expect(checkAction(spec(), action, { editing: false })).toEqual({ errors: [], warnings: [] });
  });

  it('flags unknown object type, missing tool version, bad key and duplicates', () => {
    const s = { ...spec(), actions: [action] };
    const r = checkAction(
      s,
      { ...action, key: 'Bad Key', object_type: 'x', tool_version_id: '' },
      { editing: false },
    );
    expect(r.errors).toHaveLength(3);
    expect(checkAction(s, action, { editing: false }).errors).toContain(
      'An action with this key already exists.',
    );
    expect(checkAction(s, action, { editing: true }).errors).toEqual([]);
  });

  it('covers required tool params and warns about unknown ones', () => {
    const params = toolParams({ properties: { id: {}, tier: {} }, required: ['id', 'tier'] });
    const r = checkAction(spec(), action, { editing: false, params });
    expect(r.errors).toEqual(['Required tool parameter "tier" is not mapped.']);
    expect(r.warnings).toEqual(['"note" is not a parameter of the selected tool version.']);
    expect(toolParams({})).toBeNull();
  });

  it('is part of the blocking ontology issues', () => {
    const actions: OntAction[] = [
      { key: 'a', object_type: 'nope', tool_version_id: 't', param_mapping: {} },
      {
        key: 'b',
        object_type: 'crm.customer',
        tool_version_id: 't',
        param_mapping: { p: '$object.zzz' },
      },
    ];
    expect(ontologyBlockers({ ...spec(), actions })).toHaveLength(2);
  });
});
