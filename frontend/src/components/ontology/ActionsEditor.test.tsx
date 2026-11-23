import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { OntologySpec } from '@/lib/api/ontology-types';
import { emptyOntologySpec, ontologyBlockers } from '@/lib/ontology-spec';
import { artifact } from '@/test/build';
import { stubOntology } from '@/test/ontology';
import { list } from '@/test/security';
import { jsonResponse, renderWithClient, type Handler } from '@/test/utils';
import { ActionsEditor } from './ActionsEditor';
import { removeAction, upsertAction } from '@/lib/ontology-spec';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const customer = {
  key: 'crm.customer',
  label: 'Customer',
  namespace: 'crm',
  properties: [
    {
      key: 'customer_id',
      type: 'integer' as const,
      nullable: false,
      classification: 'internal' as const,
    },
    { key: 'name', type: 'string' as const, nullable: true, classification: 'public' as const },
  ],
  primary_key: ['customer_id'],
};
const base = (): OntologySpec => ({
  ...emptyOntologySpec(),
  namespaces: [{ key: 'crm', label: 'CRM' }],
  object_types: [customer],
});

function Harness({ initial }: { initial: OntologySpec }) {
  const [spec, setSpec] = useState(initial);
  return (
    <>
      <ActionsEditor
        org="acme"
        spec={spec}
        readOnly={false}
        onUpsert={(a) => setSpec((s) => upsertAction(s, a))}
        onRemove={(k) => setSpec((s) => removeAction(s, k))}
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

const T = '/api/v1/orgs/acme/tools';
const toolRoutes = (schema: Record<string, unknown> = {}): Record<string, Handler> => ({
  [`GET ${T}`]: () => list([artifact({ id: 'tool-1', key: 'crm.escalate', name: 'Escalate' })]),
  [`GET ${T}/tool-1/versions`]: () =>
    list([
      { id: 'tv-1', version_no: 1, state: 'published' },
      { id: 'tv-2', version_no: 2, state: 'draft' },
    ]),
  [`GET ${T}/tool-1/versions/tv-1`]: () =>
    jsonResponse({ id: 'tv-1', version_no: 1, state: 'published', spec: { input_schema: schema } }),
});

async function openDialog(initial: OntologySpec, routes: Record<string, Handler>) {
  stubOntology(routes);
  renderWithClient(<Harness initial={initial} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: 'Add action' }));
  return { user, dialog: screen.getByRole('dialog') };
}

describe('actions editor', () => {
  it('adds an action with a published tool version and a mapping into the spec', async () => {
    const { user, dialog } = await openDialog(
      base(),
      toolRoutes({
        type: 'object',
        properties: { request_id: { type: 'integer' }, note: { type: 'string' } },
        required: ['request_id'],
      }),
    );
    await user.click(within(dialog).getByLabelText('Key'));
    await user.paste('crm.escalate_request');
    await user.selectOptions(within(dialog).getByLabelText('Object type'), 'crm.customer');
    await user.selectOptions(await within(dialog).findByLabelText('Tool'), 'tool-1');
    const versionSelect = await within(dialog).findByLabelText('Published tool version');
    // Select + the one published version; the draft is not offered.
    await waitFor(() => expect(within(versionSelect).getAllByRole('option')).toHaveLength(2));
    await user.selectOptions(versionSelect, 'tv-1');
    expect(await within(dialog).findByText(/required: request_id/)).toBeInTheDocument();
    // Required parameter not mapped yet: blocked.
    expect(within(dialog).getByRole('button', { name: 'Save action' })).toBeDisabled();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'Required tool parameter "request_id" is not mapped.',
    );
    await user.click(within(dialog).getByRole('button', { name: 'Add parameter' }));
    await user.click(within(dialog).getByLabelText('Parameter 1 name'));
    await user.paste('request_id');
    await user.click(within(dialog).getByLabelText('Parameter 1 value'));
    await user.paste('$object.customer_id');
    await user.click(within(dialog).getByRole('button', { name: 'Add parameter' }));
    await user.click(within(dialog).getByLabelText('Parameter 2 name'));
    await user.paste('note');
    await user.click(within(dialog).getByLabelText('Parameter 2 value'));
    await user.paste('escalated');
    await user.click(within(dialog).getByRole('button', { name: 'Save action' }));

    expect(specOf().actions).toEqual([
      {
        key: 'crm.escalate_request',
        object_type: 'crm.customer',
        tool_version_id: 'tv-1',
        param_mapping: { request_id: '$object.customer_id', note: 'escalated' },
      },
    ]);
  });

  it('rejects mappings that refer to a property the object type lacks', async () => {
    const { user, dialog } = await openDialog(base(), toolRoutes());
    await user.click(within(dialog).getByLabelText('Key'));
    await user.paste('crm.a');
    await user.selectOptions(within(dialog).getByLabelText('Object type'), 'crm.customer');
    await user.selectOptions(await within(dialog).findByLabelText('Tool'), 'tool-1');
    await user.selectOptions(
      await within(dialog).findByLabelText('Published tool version'),
      'tv-1',
    );
    await user.click(within(dialog).getByRole('button', { name: 'Add parameter' }));
    await user.click(within(dialog).getByLabelText('Parameter 1 name'));
    await user.paste('x');
    await user.click(within(dialog).getByLabelText('Parameter 1 value'));
    await user.paste('$object.nope');
    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'Mapping refers to "nope", which is not a property of crm.customer.',
    );
    expect(within(dialog).getByRole('button', { name: 'Save action' })).toBeDisabled();
  });

  it('edits and removes an existing action; the key stays immutable', async () => {
    const initial: OntologySpec = {
      ...base(),
      actions: [
        {
          key: 'crm.a',
          object_type: 'crm.customer',
          tool_version_id: 'tv-1',
          param_mapping: { id: '$object.customer_id' },
        },
      ],
    };
    stubOntology(toolRoutes());
    renderWithClient(<Harness initial={initial} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit action crm.a' }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByLabelText('Key')).toBeDisabled();
    const value = within(dialog).getByLabelText('Parameter 1 value');
    await user.clear(value);
    await user.click(value);
    await user.paste('$object.name');
    await user.click(within(dialog).getByRole('button', { name: 'Save action' }));
    expect(specOf().actions[0]?.param_mapping).toEqual({ id: '$object.name' });
    await user.click(screen.getByRole('button', { name: 'Remove action crm.a' }));
    expect(specOf().actions).toEqual([]);
  });

  it('blocks save/publish when an action points at a missing object type or property', async () => {
    const bad: OntologySpec = {
      ...base(),
      actions: [
        { key: 'crm.a', object_type: 'crm.gone', tool_version_id: 'tv-1', param_mapping: {} },
        {
          key: 'crm.b',
          object_type: 'crm.customer',
          tool_version_id: 'tv-1',
          param_mapping: { p: '$object.missing' },
        },
      ],
    };
    stubOntology(toolRoutes());
    renderWithClient(<Harness initial={bad} />);
    const blockers = within(await screen.findByRole('list', { name: 'blockers' }));
    expect(blockers.getByText(/object type "crm.gone" does not exist/)).toBeInTheDocument();
    expect(blockers.getByText(/unknown property "missing"/)).toBeInTheDocument();
  });
});
