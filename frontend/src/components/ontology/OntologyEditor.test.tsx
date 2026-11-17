import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ValidationResult } from '@/lib/api/build-types';
import type { ObjectType, OntologySpec } from '@/lib/api/ontology-types';
import { emptyOntologySpec, ontologyBlockers } from '@/lib/ontology-spec';
import { artifact } from '@/test/build';
import { stubOntology } from '@/test/ontology';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient, type Handler } from '@/test/utils';
import { OntologyEditor } from './OntologyEditor';
import type { OntologyCanvasProps } from './OntologyCanvas';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
// The real canvas needs layout measurement; here it is a probe over the editor's props.
vi.mock('./LazyOntologyCanvas', () => ({
  LazyOntologyCanvas: (p: OntologyCanvasProps) => (
    <div
      data-testid="canvas"
      data-invalid-objects={[...p.invalidObjects].sort().join(',')}
      data-invalid-rels={[...p.invalidRelationships].sort().join(',')}
    >
      <button type="button" onClick={() => p.onMoveNode('crm.customer', { x: 10.4, y: 20.6 })}>
        drag customer
      </button>
      <button type="button" onClick={() => p.onConnect('crm.customer', 'crm.account')}>
        connect customer to account
      </button>
    </div>
  ),
}));
afterEach(() => vi.unstubAllGlobals());

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
const base = (): OntologySpec => ({
  ...emptyOntologySpec(),
  namespaces: [{ key: 'crm', label: 'CRM' }],
  object_types: [customer, { ...customer, key: 'crm.account', label: 'Account' }],
});
const rel = {
  key: 'crm.customer_accounts',
  label: 'has accounts',
  from: 'crm.customer',
  to: 'crm.account',
  cardinality: 'one_to_many' as const,
  constraints: { required: false },
};

function Harness({
  initial,
  validation,
  dirty = false,
  hasDraft = true,
}: {
  initial?: OntologySpec;
  validation?: ValidationResult;
  dirty?: boolean;
  hasDraft?: boolean;
}) {
  const [spec, setSpec] = useState(initial ?? emptyOntologySpec());
  return (
    <>
      <OntologyEditor
        org="acme"
        artifact={artifact({ id: 'o-1' })}
        spec={spec}
        onChange={setSpec}
        readOnly={false}
        reportInvalid={() => undefined}
        validation={validation}
        dirty={dirty}
        hasDraft={hasDraft}
      />
      <pre data-testid="spec">{JSON.stringify(spec)}</pre>
      <ul aria-label="blockers">
        {ontologyBlockers(spec).map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </>
  );
}
const specOf = () => JSON.parse(screen.getByTestId('spec').textContent ?? '{}') as OntologySpec;

async function setup(
  props: Parameters<typeof Harness>[0] = {},
  routes: Record<string, Handler> = {},
) {
  const fetchMock = stubOntology(routes);
  renderWithClient(<Harness {...props} />);
  const user = userEvent.setup();
  await screen.findByRole('list', { name: 'Namespaces' });
  return { user, fetchMock };
}

describe('list view modelling', () => {
  it('adds an object type with a property and primary key into the spec', async () => {
    const { user } = await setup({
      initial: { ...emptyOntologySpec(), namespaces: [{ key: 'crm', label: 'CRM' }] },
    });
    await user.click(screen.getByRole('button', { name: 'List view' }));
    await user.click(screen.getByRole('button', { name: 'Add object type' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Key'), 'crm.customer');
    await user.type(within(dialog).getByLabelText('Label'), 'Customer');
    await user.click(within(dialog).getByRole('button', { name: 'Add property' }));
    const key = within(dialog).getByLabelText('Property 1 key');
    await user.clear(key);
    await user.type(key, 'customer_id');
    await user.selectOptions(within(dialog).getByLabelText('Property 1 type'), 'integer');
    await user.selectOptions(
      within(dialog).getByLabelText('Property 1 classification'),
      'restricted',
    );
    await user.click(within(dialog).getByLabelText('Property 1 nullable')); // uncheck
    await user.click(within(dialog).getByLabelText('Primary key: customer_id'));
    await user.click(within(dialog).getByRole('button', { name: 'Save object type' }));

    expect(specOf().object_types).toEqual([
      {
        key: 'crm.customer',
        label: 'Customer',
        namespace: 'crm',
        properties: [
          { key: 'customer_id', type: 'integer', nullable: false, classification: 'restricted' },
        ],
        primary_key: ['customer_id'],
      },
    ]);
    expect(
      screen.getByRole('button', { name: 'Select object type crm.customer' }),
    ).toBeInTheDocument();
  });

  it('requires a namespace before an object type can be added', async () => {
    const { user } = await setup();
    await user.click(screen.getByRole('button', { name: 'Add object type' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Add a namespace first');
    expect(screen.getByRole('button', { name: 'Save object type' })).toBeDisabled();
  });

  it('manages namespaces and refuses to remove one in use', async () => {
    const { user } = await setup({ initial: base() });
    expect(screen.getByRole('button', { name: 'Remove namespace crm' })).toBeDisabled();
    await user.type(screen.getByLabelText('Namespace key'), 'billing');
    await user.type(screen.getByLabelText('Namespace label'), 'Billing');
    await user.click(screen.getByRole('button', { name: 'Add namespace' }));
    expect(specOf().namespaces).toEqual([
      { key: 'crm', label: 'CRM' },
      { key: 'billing', label: 'Billing' },
    ]);
    await user.click(screen.getByRole('button', { name: 'Remove namespace billing' }));
    expect(specOf().namespaces).toHaveLength(1);
  });

  it('filters the graph by namespace', async () => {
    const s = base();
    s.namespaces.push({ key: 'hr', label: 'HR' });
    s.object_types.push({ ...customer, key: 'hr.employee', label: 'Employee', namespace: 'hr' });
    const { user } = await setup({ initial: s });
    await user.click(screen.getByRole('button', { name: 'List view' }));
    expect(screen.getAllByRole('button', { name: /^Select object type/ })).toHaveLength(3);
    await user.selectOptions(screen.getByLabelText('Namespace filter'), 'hr');
    expect(screen.getAllByRole('button', { name: /^Select object type/ })).toHaveLength(1);
    expect(
      screen.getByRole('button', { name: 'Select object type hr.employee' }),
    ).toBeInTheDocument();
  });

  it('writes cardinality and required constraint from the relationship dialog', async () => {
    const { user } = await setup({ initial: base() });
    await user.click(screen.getByRole('button', { name: 'Add relationship' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Key'), 'crm.customer_accounts');
    await user.type(within(dialog).getByLabelText('Label'), 'has accounts');
    await user.selectOptions(within(dialog).getByLabelText('From'), 'crm.customer');
    await user.selectOptions(within(dialog).getByLabelText('To'), 'crm.account');
    await user.selectOptions(within(dialog).getByLabelText('Cardinality'), 'many_to_many');
    await user.click(within(dialog).getByLabelText(/Required/));
    await user.click(within(dialog).getByRole('button', { name: 'Save relationship' }));
    expect(specOf().relationships).toEqual([
      { ...rel, cardinality: 'many_to_many', constraints: { required: true } },
    ]);
  });

  it('opens the relationship dialog preset when two nodes are connected on the canvas', async () => {
    const { user } = await setup({ initial: base() });
    await user.click(screen.getByRole('button', { name: 'connect customer to account' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByLabelText('From')).toHaveValue('crm.customer');
    expect(within(dialog).getByLabelText('To')).toHaveValue('crm.account');
  });

  it('persists rounded node positions to spec.ui.positions', async () => {
    const { user } = await setup({ initial: base() });
    await user.click(screen.getByRole('button', { name: 'drag customer' }));
    expect(specOf().ui.positions).toEqual({ 'crm.customer': { x: 10, y: 21 } });
  });

  it('blocks on a dangling relationship', async () => {
    const s = base();
    s.relationships = [{ ...rel, to: 'gone' }];
    await setup({ initial: s });
    expect(
      within(screen.getByRole('list', { name: 'blockers' })).getByText(/gone/),
    ).toBeInTheDocument();
  });
});

describe('validation highlighting', () => {
  it('highlights nodes and edges named by server issues', async () => {
    const s = base();
    s.relationships = [rel];
    await setup({
      initial: s,
      validation: {
        valid: false,
        errors: [
          {
            path: '',
            code: 'unmapped_object_type',
            message: 'no mapping',
            node_id: 'crm.customer',
          },
          {
            path: '',
            code: 'unmapped_relationship',
            message: 'no mapping',
            key: 'crm.customer_accounts',
          },
        ],
      },
    });
    const canvas = screen.getByTestId('canvas');
    expect(canvas).toHaveAttribute('data-invalid-objects', 'crm.customer');
    expect(canvas).toHaveAttribute('data-invalid-rels', 'crm.customer_accounts');
  });
});

describe('mapping panel', () => {
  const DISCOVERY = {
    kind: 'schema',
    tables: [
      {
        schema: 'public',
        name: 'customers',
        columns: [
          { name: 'customer_id', type: 'integer' },
          { name: 'name', type: 'integer' },
        ],
      },
    ],
  };
  const MAPPINGS = 'GET /api/v1/orgs/acme/ontologies/o-1/versions/draft/mappings';
  const routes: Record<string, Handler> = {
    'GET /api/v1/orgs/acme/connections': () =>
      list([
        { id: 'c1', name: 'Sample PG', connection_type: 'database', status: 'active' },
        { id: 'c2', name: 'API', connection_type: 'rest', status: 'active' },
        { id: 'c3', name: 'Off DB', connection_type: 'database', status: 'disabled' },
      ]),
    'GET /api/v1/orgs/acme/connections/c1/discoveries/latest': () => jsonResponse(DISCOVERY),
    [MAPPINGS]: () => list([]),
  };

  async function selectCustomer(ctx: Awaited<ReturnType<typeof setup>>) {
    await ctx.user.click(screen.getByRole('button', { name: 'List view' }));
    await ctx.user.click(screen.getByRole('button', { name: 'Select object type crm.customer' }));
  }

  it('asks to save the draft first when there are unsaved changes', async () => {
    await selectCustomer(await setup({ initial: base(), dirty: true }, routes));
    expect(await screen.findByText(/Save the draft before mapping/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save mapping' })).not.toBeInTheDocument();
  });

  it('builds the PUT body from discovery tables, flags type mismatch and says no rows are copied', async () => {
    const ctx = await setup({ initial: base() }, routes);
    const { user, fetchMock } = ctx;
    await selectCustomer(ctx);
    expect(
      await screen.findByText(/rows are read or copied into this platform/),
    ).toBeInTheDocument();
    const conn = await screen.findByLabelText('Connection');
    expect(
      within(conn)
        .getAllByRole('option')
        .map((o) => o.textContent),
    ).toEqual(['Select a database or data platform connection...', 'Sample PG (database)']);
    await user.selectOptions(conn, 'c1');
    await user.selectOptions(await screen.findByLabelText('Relation'), 'public.customers');
    // Same-named columns are prefilled; `name` (string) lands on an integer column.
    expect(screen.getByLabelText('Column for customer_id')).toHaveValue('customer_id');
    expect(
      (await screen.findAllByText(/Type mismatch: string property, integer column/)).length,
    ).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: 'Add filter' }));
    await user.type(screen.getByLabelText('Filter 1'), 'lower(name) == "x"');
    expect(screen.getByText('Function calls are not allowed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save mapping' })).toBeDisabled();
    await user.clear(screen.getByLabelText('Filter 1'));
    await user.type(screen.getByLabelText('Filter 1'), 'name != "x"');

    await user.click(screen.getByRole('button', { name: 'Save mapping' }));
    const key = 'PUT /api/v1/orgs/acme/ontologies/o-1/draft/mappings/object_type/crm.customer';
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === key)).toBe(true));
    expect(bodyOf(fetchMock, key)).toEqual({
      target_kind: 'object_type',
      target_key: 'crm.customer',
      connection_id: 'c1',
      relation: { schema: 'public', name: 'customers' },
      column_map: { customer_id: 'customer_id', name: 'name' },
      filters: ['name != "x"'],
    });
  });

  it('shows issues and sample columns from a mapping check', async () => {
    const mapped = {
      target_kind: 'object_type',
      target_key: 'crm.customer',
      connection_id: 'c1',
      relation: { schema: 'public', name: 'customers' },
      column_map: { customer_id: 'customer_id' },
      filters: [],
      status: 'invalid',
    };
    const ctx = await setup(
      { initial: base() },
      {
        ...routes,
        [MAPPINGS]: () => list([mapped]),
        'POST /api/v1/orgs/acme/ontologies/o-1/draft/mappings/object_type/crm.customer/check': () =>
          jsonResponse({
            ok: false,
            issues: [
              {
                code: 'type_incompatible',
                message: 'name: text is not integer',
                severity: 'error',
              },
              { code: 'no_discovery', message: 'columns not verified', severity: 'warning' },
            ],
            sample_columns: [{ name: 'customer_id', type: 'integer' }],
          }),
      },
    );
    await selectCustomer(ctx);
    await ctx.user.click(await screen.findByRole('button', { name: 'Check mapping' }));
    const result = await screen.findByRole('region', { name: 'Mapping check result' });
    expect(within(result).getByText('Mapping has 2 issue(s)')).toBeInTheDocument();
    expect(within(result).getByText('error')).toBeInTheDocument();
    expect(within(result).getByText('warning')).toBeInTheDocument();
    expect(within(result).getByText(/name: text is not integer/)).toBeInTheDocument();
    expect(within(result).getByText('customer_id: integer')).toBeInTheDocument();
    expect(within(result).getByText(/metadata only, no rows/)).toBeInTheDocument();
  });
});
