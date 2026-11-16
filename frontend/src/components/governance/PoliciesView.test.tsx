import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { stubGov } from '@/test/governance';
import { bodyOf, list } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { defaultDefinition, definitionErrors, PoliciesView } from './PoliciesView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const P = '/api/v1/orgs/acme/governance/policies';

describe('definition checks', () => {
  it('validates the structured kinds', () => {
    expect(definitionErrors('cost_threshold', defaultDefinition('cost_threshold'))).toEqual([]);
    expect(
      definitionErrors('cost_threshold', { threshold_amount: 0, currency: 'US' }),
    ).toHaveLength(2);
    expect(
      definitionErrors('lifecycle', {
        artifact_kinds: ['tools'],
        deprecate_unused_after_days: 1.5,
      }),
    ).toHaveLength(1);
    expect(definitionErrors('lifecycle', defaultDefinition('lifecycle'))).toEqual([]);
    expect(definitionErrors('approval_rule', {})).toHaveLength(1);
    expect(definitionErrors('approval_rule', defaultDefinition('approval_rule'))).toEqual([]);
    // currency is required in `when` when a cost limit is set
    expect(definitionErrors('approval_rule', { when: { estimated_cost_gt: 5 } })).toEqual([
      'Currency must be a 3-letter upper-case code.',
    ]);
    expect(
      definitionErrors('approval_rule', { when: { estimated_cost_gt: 5, currency: 'USD' } }),
    ).toEqual([]);
    expect(definitionErrors('model_policy', {})).toEqual([]);
  });
});

describe('PoliciesView', () => {
  it('lists policies and filters by kind', async () => {
    stubGov({
      [`GET ${P}`]: () =>
        list([
          {
            id: 'p1',
            kind: 'cost_threshold',
            name: 'Run cap',
            status: 'active',
            definition: { threshold_amount: '5', currency: 'USD', action: 'alert' },
          },
          {
            id: 'p2',
            kind: 'lifecycle',
            name: 'Stale',
            status: 'disabled',
            definition: { artifact_kinds: ['prompts'], deprecate_unused_after_days: 90 },
            enforcement: 'not_enforced',
          },
        ]),
    });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    expect(await screen.findByText('Run cap')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Kind'), 'lifecycle');
    expect(screen.queryByText('Run cap')).not.toBeInTheDocument();
    expect(screen.getByText('Stale')).toBeInTheDocument();
    // Lifecycle policies are stored and reported only; the list says so.
    expect(screen.getByText('Not enforced')).toBeInTheDocument();
  });

  it('offers every backend tool side-effect class on an approval rule', async () => {
    stubGov({ [`GET ${P}`]: () => list([]) });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Policy kind'), 'approval_rule');
    for (const c of ['none', 'read', 'write_idempotent', 'write_non_idempotent', 'destructive'])
      expect(within(dialog).getByLabelText(c)).toBeInTheDocument();
  });

  it('creates an approval rule with currency inside when (G1 shape)', async () => {
    const fetchMock = stubGov({
      [`GET ${P}`]: () => list([]),
      [`POST ${P}`]: () => jsonResponse({ id: 'p8' }, { status: 201 }),
    });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Policy kind'), 'approval_rule');
    await user.click(within(dialog).getByLabelText('destructive')); // untick the default
    await user.click(within(dialog).getByLabelText('read'));
    await user.type(
      within(dialog).getByLabelText('Require approval above estimated cost (optional)'),
      '5',
    );
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `POST ${P}`)).toBe(true));
    expect(bodyOf(fetchMock, `POST ${P}`)).toMatchObject({
      kind: 'approval_rule',
      definition: {
        when: { side_effect_classes: ['read'], estimated_cost_gt: 5, currency: 'USD' },
      },
    });
  });

  it('creates a cost threshold policy from the structured form', async () => {
    const fetchMock = stubGov({
      [`GET ${P}`]: () => list([]),
      [`POST ${P}`]: () => jsonResponse({ id: 'p9' }, { status: 201 }),
    });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dialog = screen.getByRole('dialog');
    await user.type(within(dialog).getByLabelText('Name'), 'Run cap');
    const threshold = within(dialog).getByLabelText('Threshold per run');
    await user.clear(threshold);
    await user.type(threshold, '25');
    await user.selectOptions(within(dialog).getByLabelText('When exceeded'), 'require_approval');
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `POST ${P}`)).toBe(true));
    expect(bodyOf(fetchMock, `POST ${P}`)).toEqual({
      kind: 'cost_threshold',
      name: 'Run cap',
      definition: { threshold_amount: 25, currency: 'USD', action: 'require_approval' },
      status: 'active',
    });
  });

  it('blocks an invalid structured definition', async () => {
    stubGov({ [`GET ${P}`]: () => list([]) });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dialog = screen.getByRole('dialog');
    await user.clear(within(dialog).getByLabelText('Threshold per run'));
    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'Threshold must be a positive number.',
    );
    expect(within(dialog).getByRole('button', { name: 'Create policy' })).toBeDisabled();
  });

  it('falls back to a JSON editor for model policies and rejects invalid JSON', async () => {
    const fetchMock = stubGov({
      [`GET ${P}`]: () => list([]),
      [`POST ${P}`]: () => jsonResponse({ id: 'p9' }, { status: 201 }),
    });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Create policy' }));
    const dialog = screen.getByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Policy kind'), 'model_policy');
    const json = within(dialog).getByLabelText('Definition (JSON object)');
    fireEvent.change(json, { target: { value: '{ not json' } });
    expect(within(dialog).getByText('The definition is not valid JSON.')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Create policy' })).toBeDisabled();
    fireEvent.change(json, { target: { value: '{"allowed_models":["fake-chat"]}' } });
    await user.click(within(dialog).getByRole('button', { name: 'Create policy' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `POST ${P}`)).toBe(true));
    expect(bodyOf(fetchMock, `POST ${P}`)).toMatchObject({
      kind: 'model_policy',
      definition: { allowed_models: ['fake-chat'] },
    });
  });

  it('edits with PATCH carrying row_version and preserves unknown definition keys', async () => {
    const fetchMock = stubGov({
      [`GET ${P}`]: () =>
        list([
          {
            id: 'p1',
            kind: 'lifecycle',
            name: 'Stale',
            status: 'active',
            definition: {
              artifact_kinds: ['prompts'],
              deprecate_unused_after_days: 90,
              note: 'keep',
            },
            scope: { type: 'project', id: 'proj-1' },
            row_version: 4,
          },
        ]),
      [`PATCH ${P}/p1`]: () => jsonResponse({}),
    });
    renderWithClient(<PoliciesView />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit policy Stale' }));
    const dialog = screen.getByRole('dialog');
    const days = within(dialog).getByLabelText('Deprecate versions unused for (days)');
    await user.clear(days);
    await user.type(days, '120');
    await user.click(within(dialog).getByRole('button', { name: 'Save policy' }));
    await waitFor(() => expect(fetchMock.calls.some((c) => c.key === `PATCH ${P}/p1`)).toBe(true));
    const body = bodyOf(fetchMock, `PATCH ${P}/p1`) as Record<string, unknown>;
    expect(body).toMatchObject({
      definition: {
        artifact_kinds: ['prompts'],
        deprecate_unused_after_days: 120,
        note: 'keep',
      },
      row_version: 4,
    });
    // kind and scope are immutable server-side, so they are never sent on PATCH.
    expect(body).not.toHaveProperty('kind');
    expect(body).not.toHaveProperty('scope');
  });
});
