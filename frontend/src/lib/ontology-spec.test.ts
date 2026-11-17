import { describe, expect, it } from 'vitest';
import type { ObjectType, OntologySpec } from './api/ontology-types';
import {
  buildMapping,
  checkPolicyFilter,
  draftFromMapping,
  emptyOntologySpec,
  hopOptions,
  normalizeObjectView,
  ontologyBlockers,
  ontologyIssues,
  pkOfRow,
  relatedRows,
  removeObjectType,
  setPosition,
  typeAtHop,
  typeCompat,
  validationHighlights,
} from './ontology-spec';

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
const account: ObjectType = { ...customer, key: 'crm.account', label: 'Account' };

const spec = (): OntologySpec => ({
  ...emptyOntologySpec(),
  namespaces: [{ key: 'crm', label: 'CRM' }],
  object_types: [customer, account],
  relationships: [
    {
      key: 'crm.customer_accounts',
      label: 'has accounts',
      from: 'crm.customer',
      to: 'crm.account',
      cardinality: 'one_to_many',
      constraints: { required: false },
    },
  ],
});

describe('typeCompat', () => {
  it.each([
    ['string', 'varchar(255)', 'compatible'],
    ['string', 'text', 'compatible'],
    ['integer', 'int4', 'compatible'],
    ['integer', 'bigint', 'compatible'],
    ['number', 'numeric(10,2)', 'compatible'],
    ['number', 'integer', 'compatible'],
    ['boolean', 'boolean', 'compatible'],
    ['date', 'date', 'compatible'],
    ['datetime', 'timestamp with time zone', 'compatible'],
    ['datetime', 'timestamptz', 'compatible'],
    ['json', 'jsonb', 'compatible'],
    ['integer', 'text', 'incompatible'],
    ['string', 'integer', 'incompatible'],
    ['date', 'timestamp', 'incompatible'],
    ['integer', 'numeric', 'incompatible'],
    ['string', 'geometry', 'unknown'],
    ['string', undefined, 'unknown'],
  ] as const)('%s vs %s is %s', (prop, col, expected) => {
    expect(typeCompat(prop, col)).toBe(expected);
  });
});

describe('checkPolicyFilter', () => {
  it('accepts row and principal attribute fields', () => {
    expect(checkPolicyFilter('row.region == principal.attributes.region').ok).toBe(true);
    expect(checkPolicyFilter('row.status == "open" and row.amount > 5').ok).toBe(true);
  });
  it('rejects calls, indexing and other roots', () => {
    expect(checkPolicyFilter('lower(row.region) == "x"').error).toMatch(/calls/i);
    expect(checkPolicyFilter('row["region"] == "x"').ok).toBe(false);
    expect(checkPolicyFilter('region == "x"').error).toMatch(/row\./);
    expect(checkPolicyFilter('principal.id == row.owner').ok).toBe(false);
    expect(checkPolicyFilter('row.a ==').ok).toBe(false);
  });
  it('ignores field-like text inside string literals', () => {
    expect(checkPolicyFilter('row.name == "principal.id"').ok).toBe(true);
  });
});

describe('buildMapping', () => {
  const draft = {
    ...draftFromMapping(undefined),
    connectionId: 'c1',
    relation: { schema: 'public', name: 'customers' },
    columnMap: { customer_id: 'id', name: '' },
    filters: [' status != "deleted" ', ''],
    fromColumn: 'id',
    toColumn: 'customer_id',
  };
  it('omits unmapped properties and blank filters for object types', () => {
    expect(buildMapping('object_type', 'crm.customer', draft)).toEqual({
      target_kind: 'object_type',
      target_key: 'crm.customer',
      connection_id: 'c1',
      relation: { schema: 'public', name: 'customers' },
      column_map: { customer_id: 'id' },
      filters: ['status != "deleted"'],
    });
  });
  it('builds direct and link-table key maps for relationships', () => {
    expect(buildMapping('relationship', 'r', draft).key_map).toEqual({
      from_column: 'id',
      to_column: 'customer_id',
    });
    const link = buildMapping('relationship', 'r', {
      ...draft,
      keyMode: 'link',
      linkRelation: { schema: 'public', name: 'links' },
    });
    expect(link.key_map).toEqual({
      relation: { schema: 'public', name: 'links' },
      from_column: 'id',
      to_column: 'customer_id',
    });
    expect(link.relation).toEqual({ schema: 'public', name: 'links' });
    expect(link.column_map).toEqual({});
  });
});

describe('graph edits and checks', () => {
  it('rounds positions and removes dependents with an object type', () => {
    const moved = setPosition(spec(), 'crm.customer', { x: 10.4, y: 20.6 });
    expect(moved.ui.positions['crm.customer']).toEqual({ x: 10, y: 21 });
    const gone = removeObjectType(moved, 'crm.customer');
    expect(gone.relationships).toHaveLength(0);
    expect(gone.ui.positions).toEqual({});
  });

  it('reports contract errors, blocking only structural ones', () => {
    const s = spec();
    s.object_types = [
      { ...customer, namespace: 'nope', primary_key: ['missing'] },
      { ...customer },
    ];
    s.relationships[0] = { ...s.relationships[0]!, to: 'gone' };
    const codes = ontologyIssues(s).map((i) => i.code);
    expect(codes).toEqual(
      expect.arrayContaining([
        'duplicate_key',
        'unknown_namespace',
        'invalid_primary_key',
        'dangling_relationship',
      ]),
    );
    expect(ontologyBlockers(s).length).toBeGreaterThan(0);
    expect(ontologyBlockers(spec())).toEqual([]);
  });

  it('warns without blocking on a nullable or missing primary key', () => {
    const s = spec();
    s.object_types = [{ ...customer, primary_key: [] }, account];
    expect(ontologyBlockers(s)).toEqual([]);
    expect(ontologyIssues(s).some((i) => i.severity === 'warning')).toBe(true);
  });

  it('maps validation issues to nodes and edges by key, node_id or path', () => {
    const h = validationHighlights(
      [
        { path: '', message: 'x', code: 'unmapped_object_type', node_id: 'crm.customer' },
        { path: '', message: 'y', code: 'unmapped_relationship', key: 'crm.customer_accounts' },
        { path: 'object_types[1].primary_key', message: 'z' },
        { path: 'mappings/relationship/crm.customer_accounts', message: 'w' },
      ],
      spec(),
    );
    expect([...h.objects].sort()).toEqual(['crm.account', 'crm.customer']);
    expect([...h.relationships]).toEqual(['crm.customer_accounts']);
  });
});

describe('query helpers', () => {
  it('finds traversable relationships in both directions', () => {
    const s = spec();
    expect(hopOptions(s, 'crm.customer')[0]).toMatchObject({
      target: 'crm.account',
      reverse: false,
    });
    expect(hopOptions(s, 'crm.account')[0]).toMatchObject({
      target: 'crm.customer',
      reverse: true,
    });
    expect(typeAtHop(s, 'crm.customer', [{ relationship: 'crm.customer_accounts' }])).toBe(
      'crm.account',
    );
    expect(typeAtHop(s, 'crm.customer', [{ relationship: 'nope' }])).toBeNull();
  });

  it('builds the pk path segment, composite keys comma-joined', () => {
    expect(pkOfRow(customer, { customer_id: 7 })).toBe('7');
    expect(pkOfRow(customer, { name: 'x' })).toBeNull();
    expect(
      pkOfRow({ ...customer, primary_key: ['customer_id', 'name'] }, { customer_id: 1, name: 'a' }),
    ).toBe('1,a');
  });

  it('normalizes object views with map or list relationship summaries', () => {
    expect(
      normalizeObjectView({
        object: { a: 1 },
        relationships: { r1: 3, r2: { count: 5, capped: true } },
      }),
    ).toEqual({
      properties: { a: 1 },
      relationships: [
        { relationship: 'r1', count: 3 },
        { relationship: 'r2', count: 5, capped: true },
      ],
    });
    expect(normalizeObjectView({ a: 1, relationships: [{ relationship: 'r', count: 2 }] })).toEqual(
      { properties: { a: 1 }, relationships: [{ relationship: 'r', count: 2 }] },
    );
  });

  it('keeps label, direction and capped from the backend object view', () => {
    expect(
      normalizeObjectView({
        object: { a: 1 },
        relationships: [
          { key: 'r', label: 'has r', direction: 'reverse', count: 100, capped: true },
          { key: 'q', label: 'q', direction: 'sideways', count: 1, capped: false },
        ],
        lineage_id: 'l',
      }),
    ).toEqual({
      properties: { a: 1 },
      relationships: [
        { relationship: 'r', count: 100, capped: true, label: 'has r', direction: 'reverse' },
        { relationship: 'q', count: 1, label: 'q' },
      ],
    });
  });

  it('reads related rows from the row or its _links', () => {
    expect(relatedRows({ r: [{ id: 1 }] }, 'r')).toEqual([{ id: 1 }]);
    expect(relatedRows({ _links: { r: [{ id: 2 }] } }, 'r')).toEqual([{ id: 2 }]);
    expect(relatedRows({}, 'r')).toEqual([]);
  });
});
