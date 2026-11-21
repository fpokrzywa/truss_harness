import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DATASET, DS, RELEASE_READ_PERMS, stubReleases } from '@/test/releases';
import { ALL_PERMS, bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { DatasetsView } from './DatasetsView';

const nav = vi.hoisted(() => ({ query: 'ws=eng&proj=api' }));
vi.mock('next/navigation', () => ({
  useParams: () => ({ org: 'acme' }),
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/o/acme/evaluations/datasets',
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));
beforeEach(() => {
  nav.query = 'ws=eng&proj=api';
});
afterEach(() => vi.unstubAllGlobals());

const CASES = [
  {
    case_id: 'c1',
    input: { message: 'hi' },
    assertions: [{ id: 'a1', kind: 'contains', value: 'hello' }],
  },
  {
    case_id: 'c2',
    input: { message: 'bye' },
    assertions: [{ id: 'a1', kind: 'equals', value: 'bye' }],
  },
];

/** Sets a textarea's value in one event (typing JSON with userEvent would parse its braces). */
const paste = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });

describe('DatasetsView', () => {
  it('lists the chosen project datasets with name, version, case count and short digest', async () => {
    stubReleases();
    renderWithClient(<DatasetsView />);
    const table = await screen.findByRole('table', { name: 'Evaluation datasets' });
    const row = within(table).getByText('Sales FAQ').closest('tr')!;
    expect(row).toHaveTextContent('v2');
    expect(within(row).getByText('3')).toBeInTheDocument();
    const digest = within(row).getByText('e'.repeat(12));
    expect(digest).toHaveAttribute('title', DATASET.digest);
  });

  it('asks for a project and lists nothing without ?ws=&proj=', async () => {
    nav.query = '';
    const fetchMock = stubReleases();
    renderWithClient(<DatasetsView />);
    expect(await screen.findByText(/Choose a workspace and a project/)).toBeInTheDocument();
    await screen.findByRole('option', { name: 'Engineering' });
    expect(fetchMock.calls.some((c) => c.key.includes('evaluation-datasets'))).toBe(false);
  });

  it('has no New dataset version button without evaluation:dataset:write', async () => {
    stubReleases({}, [...ALL_PERMS, ...RELEASE_READ_PERMS]);
    renderWithClient(<DatasetsView />);
    await screen.findByRole('table', { name: 'Evaluation datasets' });
    expect(screen.queryByRole('button', { name: /New/ })).not.toBeInTheDocument();
  });

  it('shows the JSON error for pasted invalid JSON and sends no POST', async () => {
    const fetchMock = stubReleases();
    renderWithClient(<DatasetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'New dataset version' }));
    const d = within(screen.getByRole('dialog'));
    await userEvent.type(d.getByLabelText('Name'), 'Sales FAQ');
    paste(d.getByLabelText('Cases (JSON)'), '[{"case_id": ');
    await userEvent.click(d.getByRole('button', { name: 'Create dataset version' }));
    expect(await d.findByText('The cases are not valid JSON.')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('reads an uploaded file and refuses a duplicate case_id without posting', async () => {
    const fetchMock = stubReleases();
    renderWithClient(<DatasetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'New version of Sales FAQ' }));
    const d = within(screen.getByRole('dialog'));
    expect(d.getByLabelText('Name')).toHaveValue('Sales FAQ');
    const json = JSON.stringify([CASES[0], CASES[0]]);
    const file = new File([json], 'cases.json', { type: 'application/json' });
    await userEvent.upload(d.getByLabelText('Or upload a .json file'), file);
    await waitFor(() => expect(d.getByLabelText('Cases (JSON)')).toHaveValue(json));
    await userEvent.click(d.getByRole('button', { name: 'Create dataset version' }));
    expect(await d.findByText('Duplicate case_id "c1".')).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('posts valid cases with the name and refreshes the list', async () => {
    const fetchMock = stubReleases({
      [`POST ${DS}`]: () => jsonResponse({ ...DATASET, version_no: 3 }, { status: 201 }),
    });
    renderWithClient(<DatasetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'New dataset version' }));
    const d = within(screen.getByRole('dialog'));
    await userEvent.type(d.getByLabelText('Name'), 'Sales FAQ');
    paste(d.getByLabelText('Cases (JSON)'), JSON.stringify(CASES, null, 2));
    await userEvent.click(d.getByRole('button', { name: 'Create dataset version' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${DS}`)).toEqual({ name: 'Sales FAQ', cases: CASES });
    await waitFor(() =>
      expect(fetchMock.calls.filter((c) => c.key === `GET ${DS}`).length).toBe(2),
    );
  });

  it('shows server field errors through the error alert', async () => {
    stubReleases({
      [`POST ${DS}`]: () =>
        problemResponse(422, 'validation_failed', {
          errors: [
            {
              loc: ['body', 'cases', 0, 'assertions', 0, 'value'],
              msg: 'not a valid regular expression',
              type: 'value_error',
            },
          ],
        }),
    });
    renderWithClient(<DatasetsView />);
    await userEvent.click(await screen.findByRole('button', { name: 'New dataset version' }));
    const d = within(screen.getByRole('dialog'));
    await userEvent.type(d.getByLabelText('Name'), 'Sales FAQ');
    paste(d.getByLabelText('Cases (JSON)'), JSON.stringify(CASES));
    await userEvent.click(d.getByRole('button', { name: 'Create dataset version' }));
    const alert = await d.findByRole('alert');
    expect(alert).toHaveTextContent('Could not create the dataset');
    expect(alert).toHaveTextContent('not a valid regular expression');
  });
});
