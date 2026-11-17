import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { OntologySpec } from '@/lib/api/ontology-types';
import { emptyOntologySpec } from '@/lib/ontology-spec';
import { artifact, version } from '@/test/build';
import { stubOntology } from '@/test/ontology';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient, type Handler } from '@/test/utils';
import { ActionsPanel } from './ActionsPanel';
import { ExplorerPanel } from './ExplorerPanel';
import { LineagePanel } from './LineagePanel';
import { PoliciesPanel } from './PoliciesPanel';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const A = artifact({ id: 'o-1', latest_published_version: { id: 'v-2', version_no: 2 } });
const O = '/api/v1/orgs/acme/ontologies/o-1';

const prop = (key: string, type: 'string' | 'integer', classification = 'internal') => ({
  key,
  type,
  nullable: false,
  classification,
});
const SPEC: OntologySpec = {
  ...emptyOntologySpec(),
  namespaces: [{ key: 'crm', label: 'CRM' }],
  object_types: [
    {
      key: 'crm.customer',
      label: 'Customer',
      namespace: 'crm',
      properties: [
        prop('customer_id', 'integer'),
        prop('name', 'string'),
        prop('tax_id', 'string', 'restricted'),
      ] as OntologySpec['object_types'][number]['properties'],
      primary_key: ['customer_id'],
    },
    {
      key: 'crm.account',
      label: 'Account',
      namespace: 'crm',
      properties: [
        prop('account_id', 'integer'),
      ] as OntologySpec['object_types'][number]['properties'],
      primary_key: ['account_id'],
    },
  ],
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
  actions: [
    {
      key: 'crm.escalate',
      object_type: 'crm.customer',
      tool_version_id: 'tv-1',
      param_mapping: { customer_id: '$object.customer_id' },
    },
  ],
};

const versionRoutes: Record<string, Handler> = {
  [`GET ${O}/versions`]: () => list([version({ id: 'v-2', version_no: 2 })]),
  [`GET ${O}/versions/v-2`]: () =>
    jsonResponse(version({ id: 'v-2', version_no: 2, spec: SPEC as never })),
  [`GET ${O}/draft`]: () =>
    jsonResponse(version({ id: 'd-1', state: 'draft', spec: SPEC as never, row_version: 1 })),
};

describe('PoliciesPanel', () => {
  const policies: Handler = () =>
    list([
      {
        id: 'p1',
        object_type_key: 'crm.customer',
        property_keys: null,
        subject: { type: 'role', id: 'r1' },
        effect: 'allow',
        row_filter: 'row.region == principal.attributes.region',
      },
    ]);
  const routes: Record<string, Handler> = {
    ...versionRoutes,
    [`GET ${O}/policies`]: policies,
    'GET /api/v1/orgs/acme/roles': () =>
      list([
        {
          id: 'r1',
          key: 'dev',
          name: 'Developer',
          is_system: true,
          permissions: [],
          row_version: 1,
        },
      ]),
  };

  it('lists policies with resolved subject names', async () => {
    stubOntology(routes);
    renderWithClient(<PoliciesPanel org="acme" artifact={A} />);
    expect(await screen.findByText('role: Developer')).toBeInTheDocument();
    expect(screen.getByText('All non-restricted properties')).toBeInTheDocument();
  });

  it('rejects a row filter with calls and sends no request', async () => {
    const fetchMock = stubOntology(routes);
    renderWithClient(<PoliciesPanel org="acme" artifact={A} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Add policy' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(await within(dialog).findByLabelText('Subject'), 'r1');
    await user.type(within(dialog).getByLabelText('Row filter'), 'lower(row.region) == "x"');
    expect(within(dialog).getByText('Function calls are not allowed.')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Save policy' })).toBeDisabled();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
    await user.clear(within(dialog).getByLabelText('Row filter'));
    await user.type(within(dialog).getByLabelText('Row filter'), 'status == "x"');
    expect(within(dialog).getByText(/Field "status" is not allowed/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Save policy' })).toBeDisabled();
  });

  it('creates a policy, warning only when restricted properties are named', async () => {
    const fetchMock = stubOntology({
      ...routes,
      [`POST ${O}/policies`]: () => jsonResponse({ id: 'p2' }, { status: 201 }),
    });
    renderWithClient(<PoliciesPanel org="acme" artifact={A} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Add policy' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(await within(dialog).findByLabelText('Subject'), 'r1');
    // "All non-restricted properties" never grants tax_id: a note, not a warning.
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    expect(
      within(dialog).getByText(/Restricted properties \(tax_id\) are not covered/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText('All non-restricted properties'));
    await user.click(within(dialog).getByLabelText('name'));
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    await user.click(within(dialog).getByLabelText(/tax_id/));
    expect(within(dialog).getByRole('alert')).toHaveTextContent('restricted properties (tax_id)');
    await user.click(within(dialog).getByLabelText(/tax_id/));
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    await user.type(
      within(dialog).getByLabelText('Row filter'),
      'row.region == principal.attributes.region',
    );
    await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
    await waitFor(() =>
      expect(bodyOf(fetchMock, `POST ${O}/policies`)).toEqual({
        object_type_key: 'crm.customer',
        property_keys: ['name'],
        subject: { type: 'role', id: 'r1' },
        effect: 'allow',
        row_filter: 'row.region == principal.attributes.region',
      }),
    );
  });
});

describe('Principal attributes editor', () => {
  const ATTR = '/api/v1/orgs/acme/ontology-principal-attributes/user/u1';
  const usersRoute: Handler = () =>
    list([
      {
        user: { id: 'u1', email: 'ada@x.io', display_name: 'Ada' },
        status: 'active',
        roles: [],
      },
    ]);

  async function open(routes: Record<string, Handler>) {
    const fetchMock = stubOntology({
      ...versionRoutes,
      [`GET ${O}/policies`]: () => list([]),
      'GET /api/v1/orgs/acme/users': usersRoute,
      ...routes,
    });
    renderWithClient(<PoliciesPanel org="acme" artifact={A} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Principal attributes' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(await within(dialog).findByLabelText('Principal'), 'u1');
    await user.click(within(dialog).getByRole('button', { name: 'Load attributes' }));
    return { fetchMock, user, dialog };
  }

  it('loads, edits and saves attributes with the row version', async () => {
    const { fetchMock, user, dialog } = await open({
      [`GET ${ATTR}`]: () =>
        jsonResponse({
          principal_type: 'user',
          principal_id: 'u1',
          attributes: { region: 'emea' },
          row_version: 3,
        }),
      [`PUT ${ATTR}`]: () =>
        jsonResponse({
          principal_type: 'user',
          principal_id: 'u1',
          attributes: { region: 'apac' },
          row_version: 4,
        }),
    });
    const box = await within(dialog).findByLabelText('Attributes (JSON object)');
    expect(box).toHaveValue(JSON.stringify({ region: 'emea' }, null, 2));
    await user.clear(box);
    await user.click(box);
    await user.paste('{"region": "apac"}');
    await user.click(within(dialog).getByRole('button', { name: 'Save attributes' }));
    expect(await within(dialog).findByText('Attributes saved.')).toBeInTheDocument();
    expect(bodyOf(fetchMock, `PUT ${ATTR}`)).toEqual({
      attributes: { region: 'apac' },
      row_version: 3,
    });
  });

  it('treats 404 as no attributes yet and rejects invalid maps client-side', async () => {
    const { fetchMock, user, dialog } = await open({
      [`GET ${ATTR}`]: () => problemResponse(404, 'not_found'),
    });
    const box = await within(dialog).findByLabelText('Attributes (JSON object)');
    expect(box).toHaveValue('{}');
    await user.clear(box);
    await user.click(box);
    await user.paste('{"re gion": "x"}');
    expect(within(dialog).getByText(/may only use letters, digits and _/)).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Save attributes' })).toBeDisabled();
    await user.clear(box);
    await user.click(box);
    await user.paste('{"region": {"a": 1}}');
    expect(
      within(dialog).getByText(/must be a string, number, boolean or a list/),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('PUT'))).toBe(false);
  });
});

describe('ExplorerPanel', () => {
  const QUERY = `POST ${O}/query`;

  async function pickCustomer() {
    renderWithClient(<ExplorerPanel org="acme" artifact={A} />);
    const user = userEvent.setup();
    expect(await screen.findByText(/Materialization is not enabled/)).toBeInTheDocument();
    await user.selectOptions(await screen.findByLabelText('Object type'), 'crm.customer');
    return user;
  }

  it('builds a query with traversal and caps depth at three hops', async () => {
    const fetchMock = stubOntology({
      ...versionRoutes,
      [QUERY]: () => jsonResponse({ rows: [], lineage_id: 'lin-1' }),
    });
    const user = await pickCustomer();
    await user.click(screen.getByLabelText('name'));
    await user.type(screen.getByLabelText('Filter'), 'name != "x"');
    await user.click(screen.getByRole('button', { name: 'Add hop' }));
    await user.selectOptions(screen.getByLabelText('Hop 1 relationship'), 'crm.customer_accounts');
    // Hop 1 properties default to the target primary key.
    expect(screen.getByLabelText('account_id')).toBeChecked();
    // Reverse hops back to customer are offered; add two more to hit the cap.
    await user.click(screen.getByRole('button', { name: 'Add hop' }));
    await user.selectOptions(screen.getByLabelText('Hop 2 relationship'), 'crm.customer_accounts');
    await user.click(screen.getByRole('button', { name: 'Add hop' }));
    await user.selectOptions(screen.getByLabelText('Hop 3 relationship'), 'crm.customer_accounts');
    expect(screen.getByRole('button', { name: 'Add hop' })).toBeDisabled();
    expect(screen.getByText('3 of 3 hops')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Run query' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === QUERY)).toBe(true));
    const body = bodyOf(fetchMock, QUERY) as { traverse: unknown[] };
    expect(body).toMatchObject({
      version_id: 'v-2',
      object_type: 'crm.customer',
      select: ['customer_id', 'name'],
      filter: 'name != "x"',
      limit: 50,
    });
    expect(body.traverse).toHaveLength(3);
    // Contract F1: reverse hops are sent as direction "reverse" (account back to customer).
    expect(body.traverse[1]).toMatchObject({ direction: 'reverse' });
    expect(body.traverse[2]).toMatchObject({ direction: 'forward' });
    expect(body.traverse[0]).toEqual({
      relationship: 'crm.customer_accounts',
      direction: 'forward',
      select: ['account_id'],
    });
    expect(await screen.findByRole('link', { name: 'Lineage lin-1' })).toHaveAttribute(
      'href',
      '/o/acme/build/ontologies/o-1/lineage?lineage_id=lin-1',
    );
  });

  it('shows a 403 property_denied clearly instead of dropping the property', async () => {
    stubOntology({
      ...versionRoutes,
      [QUERY]: () =>
        problemResponse(403, 'property_denied', { detail: 'Property tax_id is not permitted.' }),
    });
    const user = await pickCustomer();
    await user.click(screen.getByLabelText('tax_id'));
    await user.click(screen.getByRole('button', { name: 'Run query' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Property access denied');
    expect(alert).toHaveTextContent('Property tax_id is not permitted.');
    expect(screen.queryByRole('region', { name: 'Results' })).not.toBeInTheDocument();
  });

  it('rejects an invalid filter before calling the API', async () => {
    const fetchMock = stubOntology({ ...versionRoutes });
    const user = await pickCustomer();
    await user.type(screen.getByLabelText('Filter'), 'lower(name) == "x"');
    expect(screen.getByRole('button', { name: 'Run query' })).toBeDisabled();
    expect(fetchMock.calls.some((c) => c.key === QUERY)).toBe(false);
  });

  it('loads more with the cursor, then navigates object to related object', async () => {
    let call = 0;
    const fetchMock = stubOntology({
      ...versionRoutes,
      [QUERY]: ({ init }) => {
        const body = JSON.parse(init.body as string) as { cursor?: string; traverse?: unknown };
        if (body.traverse)
          return jsonResponse({
            rows: [{ customer_id: 1, _links: { 'crm.customer_accounts': [{ account_id: 9 }] } }],
            lineage_id: 'lin-n',
          });
        call += 1;
        return body.cursor
          ? jsonResponse({ rows: [{ customer_id: 2 }], lineage_id: 'lin-2' })
          : jsonResponse({ rows: [{ customer_id: 1 }], next_cursor: 'c2', lineage_id: 'lin-1' });
      },
      [`GET ${O}/objects/crm.customer/1`]: () =>
        jsonResponse({ object: { customer_id: 1 }, relationships: { 'crm.customer_accounts': 1 } }),
      [`GET ${O}/objects/crm.account/9`]: () =>
        jsonResponse({ object: { account_id: 9 }, relationships: [] }),
    });
    const user = await pickCustomer();
    await user.click(screen.getByRole('button', { name: 'Run query' }));
    await screen.findByText('1 row loaded');
    await user.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('2 rows loaded');
    expect(call).toBe(2);
    expect(
      JSON.parse(fetchMock.calls.filter((c) => c.key === QUERY)[1]?.init.body as string),
    ).toMatchObject({
      cursor: 'c2',
    });
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Open crm.customer 1' }));
    expect(await screen.findByRole('group', { name: 'Object crm.customer 1' })).toBeInTheDocument();
    const summaries = await screen.findByRole('list', { name: 'Relationship summaries' });
    expect(within(summaries).getByText('1 related')).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', { name: 'View related via crm.customer_accounts' }),
    );
    // Related rows come from row._links and the request names the direction.
    const related = fetchMock.calls
      .filter((c) => c.key === QUERY)
      .map((c) => JSON.parse(c.init.body as string) as { traverse?: { direction: string }[] })
      .find((b) => b.traverse);
    expect(related?.traverse?.[0]?.direction).toBe('forward');
    await user.click(await screen.findByRole('button', { name: 'Open crm.account 9' }));
    expect(await screen.findByRole('group', { name: 'Object crm.account 9' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(await screen.findByRole('group', { name: 'Object crm.customer 1' })).toBeInTheDocument();
  });
});

describe('Object view additive fields', () => {
  it('shows relationship label, direction and capped, and the truncated notice', async () => {
    const fetchMock = stubOntology({
      ...versionRoutes,
      [`POST ${O}/query`]: ({ init }) => {
        const body = JSON.parse(init.body as string) as { traverse?: unknown };
        return body.traverse
          ? jsonResponse({
              rows: [{ customer_id: 1, _links: { 'crm.customer_accounts': [{ account_id: 9 }] } }],
              lineage_id: 'lin-n',
              truncated: true,
            })
          : jsonResponse({ rows: [{ customer_id: 1 }], lineage_id: 'lin-1' });
      },
      [`GET ${O}/objects/crm.customer/1`]: () =>
        jsonResponse({
          object: { customer_id: 1 },
          relationships: [
            {
              key: 'crm.customer_accounts',
              label: 'has accounts',
              direction: 'forward',
              count: 100,
              capped: true,
            },
          ],
          lineage_id: 'lin-o',
        }),
    });
    renderWithClient(<ExplorerPanel org="acme" artifact={A} />);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText('Object type'), 'crm.customer');
    await user.click(screen.getByRole('button', { name: 'Run query' }));
    await user.click(await screen.findByRole('button', { name: 'Open crm.customer 1' }));
    const summaries = await screen.findByRole('list', { name: 'Relationship summaries' });
    expect(within(summaries).getByText('has accounts')).toBeInTheDocument();
    expect(within(summaries).getByText('forward')).toBeInTheDocument();
    expect(within(summaries).getByText('100+ related')).toBeInTheDocument();
    expect(within(summaries).getByText('capped')).toBeInTheDocument();
    // The object GET pins the explorer's version.
    expect(
      fetchMock.calls.some((c) => c.key.startsWith('GET') && c.url.includes('version_id=v-2')),
    ).toBe(true);
    await user.click(
      screen.getByRole('button', { name: 'View related via crm.customer_accounts' }),
    );
    expect(await screen.findByText(/truncated by a server cap/)).toBeInTheDocument();
  });
});

describe('LineagePanel', () => {
  it('lists hashes and counts, never values, and pages with a cursor', async () => {
    const rec = (id: string) => ({
      id,
      at: '2026-01-01T00:00:00Z',
      principal: { type: 'user', id: 'u1', display: 'Ada' },
      run_id: 'run-1',
      ontology_version_id: 'v-2',
      object_types: ['crm.customer'],
      connections: ['c1'],
      compiled_query_hashes: ['9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08'],
      row_counts: { 'crm.customer': 12 },
      duration_ms: 42,
    });
    stubOntology({
      [`GET /api/v1/orgs/acme/ontology-lineage`]: ({ url }) =>
        url.includes('cursor=n2') ? list([rec('l2')]) : list([rec('l1')], 'n2'),
    });
    renderWithClient(<LineagePanel org="acme" artifact={A} highlight="l1" />);
    const table = await screen.findByRole('table');
    expect(within(table).getByText('Ada')).toBeInTheDocument();
    expect(within(table).getByText('crm.customer: 12')).toBeInTheDocument();
    expect(within(table).getByText('42 ms')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: 'run-1' })).toHaveAttribute(
      'href',
      '/o/acme/runs/run-1',
    );
    // Hash is truncated for display and carries the full value only in the title.
    expect(within(table).getByText('9f86d081884c7d65...')).toBeInTheDocument();
    expect(within(table).getByText('(selected)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await waitFor(() =>
      expect(within(screen.getByRole('table')).getAllByText('Ada')).toHaveLength(2),
    );
  });
});

const PA = artifact({
  id: 'o-1',
  scope: { type: 'project', id: 'p-1' },
  latest_published_version: { id: 'v-2', version_no: 2 },
});

describe('ActionsPanel', () => {
  const INVOKE = `POST ${O}/actions/crm.escalate/invoke`;
  const run = (status: string) => ({ id: 'run-9', status, kind: 'ontology_action' });

  async function invoke(status: string) {
    const fetchMock = stubOntology({
      ...versionRoutes,
      [INVOKE]: () => jsonResponse(run(status), { status: 202 }),
    });
    renderWithClient(<ActionsPanel org="acme" artifact={PA} canInvoke />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Invoke crm.escalate' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Object primary key'), '1');
    await user.click(within(dialog).getByRole('button', { name: 'Invoke' }));
    return { fetchMock, dialog };
  }

  it('creates a run and links to it', async () => {
    const { fetchMock, dialog } = await invoke('pending');
    const link = await within(dialog).findByRole('link', { name: 'run-9' });
    expect(link).toHaveAttribute('href', '/o/acme/runs/run-9');
    expect(bodyOf(fetchMock, INVOKE)).toEqual({ object_pk: '1' });
    expect(within(dialog).queryByText(/waiting for approval/)).not.toBeInTheDocument();
  });

  it('explains approval when the run is waiting_for_approval', async () => {
    const { dialog } = await invoke('waiting_for_approval');
    expect(await within(dialog).findByText(/waiting for approval/)).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Approvals' })).toHaveAttribute(
      'href',
      '/o/acme/approvals',
    );
  });

  it('sends extra params and rejects non-object JSON', async () => {
    stubOntology({ ...versionRoutes });
    renderWithClient(<ActionsPanel org="acme" artifact={PA} canInvoke />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Invoke crm.escalate' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Object primary key'), '1');
    await user.type(within(dialog).getByLabelText(/Extra parameters/), '[[1]');
    expect(within(dialog).getByText('Extra parameters must be a JSON object.')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Invoke' })).toBeDisabled();
  });

  it('project-scoped ontologies need no workspace or project', async () => {
    const fetchMock = stubOntology({ ...versionRoutes });
    renderWithClient(<ActionsPanel org="acme" artifact={PA} canInvoke />);
    await userEvent.click(await screen.findByRole('button', { name: 'Invoke crm.escalate' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).queryByLabelText('Workspace')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Project')).not.toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.endsWith('/workspaces'))).toBe(false);
  });

  const WS = { id: 'ws-1', slug: 'ops', name: 'Ops', status: 'active', row_version: 1 };
  const scopeRoutes: Record<string, Handler> = {
    ...versionRoutes,
    'GET /api/v1/orgs/acme/workspaces': () =>
      list([WS, { ...WS, id: 'ws-2', slug: 'dev', name: 'Dev' }]),
    'GET /api/v1/orgs/acme/workspaces/ops/projects': () =>
      list([{ ...WS, id: 'p-1', slug: 'portal', name: 'Portal' }]),
    'GET /api/v1/orgs/acme/workspaces/dev/projects': () => list([]),
  };

  it('tenant-scoped ontologies must pick a workspace and project and send their slugs', async () => {
    const fetchMock = stubOntology({
      ...scopeRoutes,
      [INVOKE]: () => jsonResponse(run('pending'), { status: 202 }),
    });
    renderWithClient(<ActionsPanel org="acme" artifact={A} canInvoke />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Invoke crm.escalate' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Object primary key'), '1');
    const submit = within(dialog).getByRole('button', { name: 'Invoke' });
    expect(submit).toBeDisabled();
    await within(dialog).findByRole('option', { name: /Ops/ });
    await user.selectOptions(within(dialog).getByLabelText('Workspace'), 'ops');
    expect(submit).toBeDisabled();
    await within(dialog).findByRole('option', { name: /Portal/ });
    await user.selectOptions(within(dialog).getByLabelText('Project'), 'portal');
    await user.click(submit);
    await within(dialog).findByRole('link', { name: 'run-9' });
    expect(bodyOf(fetchMock, INVOKE)).toEqual({
      object_pk: '1',
      workspace: 'ops',
      project: 'portal',
    });
  });

  it('workspace-scoped ontologies prefill and lock their workspace', async () => {
    const fetchMock = stubOntology({
      ...scopeRoutes,
      [INVOKE]: () => jsonResponse(run('pending'), { status: 202 }),
    });
    const WA = artifact({
      id: 'o-1',
      scope: { type: 'workspace', id: 'ws-1' },
      latest_published_version: { id: 'v-2', version_no: 2 },
    });
    renderWithClient(<ActionsPanel org="acme" artifact={WA} canInvoke />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Invoke crm.escalate' }));
    const dialog = screen.getByRole('dialog');
    await waitFor(() => expect(within(dialog).getByLabelText('Workspace')).toHaveValue('ops'));
    expect(within(dialog).getByLabelText('Workspace')).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Object primary key'), '7');
    await within(dialog).findByRole('option', { name: /Portal/ });
    await user.selectOptions(within(dialog).getByLabelText('Project'), 'portal');
    await user.click(within(dialog).getByRole('button', { name: 'Invoke' }));
    await within(dialog).findByRole('link', { name: 'run-9' });
    expect(bodyOf(fetchMock, INVOKE)).toEqual({
      object_pk: '7',
      workspace: 'ops',
      project: 'portal',
    });
  });

  it('hides invoke without ontology:query', async () => {
    stubOntology({ ...versionRoutes });
    renderWithClient(<ActionsPanel org="acme" artifact={A} canInvoke={false} />);
    await screen.findByRole('list', { name: 'Actions' });
    expect(screen.queryByRole('button', { name: 'Invoke crm.escalate' })).not.toBeInTheDocument();
  });
});
