import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  EVALUATION,
  list,
  ORG,
  QUEUED,
  RELEASE,
  RELEASE_DIGEST,
  RELEASE_PERMS,
  stubReleases,
} from '@/test/releases';
import { ALL_PERMS, bodyOf } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient } from '@/test/utils';
import { ReleaseDetailView } from './ReleaseDetailView';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

const R = `${ORG}/releases/r-1`;

describe('ReleaseDetailView', () => {
  it('renders the digest, manifest, compatibility and the evaluations with links', async () => {
    stubReleases();
    renderWithClient(<ReleaseDetailView id="r-1" />);
    expect(await screen.findByText(RELEASE_DIGEST)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'v1' })).toHaveAttribute(
      'href',
      '/o/acme/solutions/s-1?ws=eng&proj=api',
    );
    const manifest = screen.getByRole('region', { name: 'Manifest' });
    const m = within(manifest);
    expect(m.getByText('databricks.app/v1')).toBeInTheDocument();
    expect(m.getByText('av-1', { selector: 'dd span' })).toBeInTheDocument();
    const pins = m.getByRole('table', { name: 'Pins' });
    expect(within(pins).getByText('tool').closest('tr')).toHaveTextContent('tv-1');
    expect(m.getByText('conn-db').closest('li')).toHaveTextContent(
      'conn-db: sales.orders, sales.customers',
    );
    expect(m.getByText('m-1').closest('li')).toHaveTextContent('m-1: gpt-prod (provider prov-1)');
    expect(m.getByText('conn-crm').closest('li')).toHaveTextContent('conn-crm: conn-crm-prod');
    expect(m.getByText('ds-1')).toBeInTheDocument();
    expect(m.getByText('0.9')).toBeInTheDocument();
    expect(m.getByText('2000')).toBeInTheDocument();
    expect(m.getByText('1.5')).toBeInTheDocument();
    expect(m.getByText('shifde-compiler/1')).toBeInTheDocument();
    const compat = screen.getByRole('table', { name: 'Compatibility' });
    expect(within(compat).getAllByText('Supported')).toHaveLength(2);
    const evaluations = await screen.findByRole('table', { name: 'Evaluations' });
    const row = within(evaluations).getByText('failed').closest('tr')!;
    expect(row).toHaveTextContent('0.5');
    expect(row).toHaveTextContent('1200');
    expect(row).toHaveTextContent('0.42');
    expect(within(row).getByRole('link')).toHaveAttribute('href', '/o/acme/evaluations/ev-1');
  });

  it('shows - for manifest items that are absent', async () => {
    stubReleases({ [`GET ${R}`]: () => jsonResponse({ ...RELEASE, manifest: {} }) });
    renderWithClient(<ReleaseDetailView id="r-1" />);
    const manifest = await screen.findByRole('region', { name: 'Manifest' });
    expect(within(manifest).getAllByText('-').length).toBeGreaterThanOrEqual(8);
    expect(within(manifest).getByText('No pins.')).toBeInTheDocument();
  });

  it('runs an evaluation with {} and refreshes the list', async () => {
    let runs = [EVALUATION];
    const fetchMock = stubReleases({
      [`GET ${R}/evaluations`]: () => list(runs),
      [`POST ${R}/evaluations`]: () => {
        runs = [QUEUED, EVALUATION];
        return jsonResponse(QUEUED, { status: 201 });
      },
    });
    renderWithClient(<ReleaseDetailView id="r-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Run evaluation' }));
    await waitFor(() => expect(bodyOf(fetchMock, `POST ${R}/evaluations`)).toEqual({}));
    const evaluations = screen.getByRole('table', { name: 'Evaluations' });
    expect(await within(evaluations).findByText('queued')).toBeInTheDocument();
  });

  it('shows a 409 release_withdrawn refusal in an alert', async () => {
    stubReleases({
      [`POST ${R}/evaluations`]: () =>
        problemResponse(409, 'release_withdrawn', { detail: 'The release is withdrawn.' }),
    });
    renderWithClient(<ReleaseDetailView id="r-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Run evaluation' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not request the evaluation');
    expect(alert).toHaveTextContent('The release is withdrawn.');
  });

  it('shows the no-access message on a 404', async () => {
    stubReleases({ [`GET ${R}`]: () => problemResponse(404, 'not_found') });
    renderWithClient(<ReleaseDetailView id="r-1" />);
    expect(await screen.findByText('Not found or no access')).toBeInTheDocument();
  });
});

describe('ReleaseDetailView withdraw', () => {
  it('needs a note and sends nothing without one', async () => {
    const fetchMock = stubReleases();
    renderWithClient(<ReleaseDetailView id="r-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Withdraw' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Withdraw release' }));
    expect(
      await within(dialog).findByText('A note of 1 to 2000 characters is required.'),
    ).toBeInTheDocument();
    expect(fetchMock.calls.some((c) => c.key.startsWith('POST'))).toBe(false);
  });

  it('posts the note, then shows withdrawn without Withdraw or Run evaluation', async () => {
    let release = RELEASE;
    const fetchMock = stubReleases({
      [`GET ${R}`]: () => jsonResponse(release),
      [`POST ${R}/withdraw`]: () => {
        release = { ...RELEASE, status: 'withdrawn' };
        return jsonResponse(release);
      },
    });
    renderWithClient(<ReleaseDetailView id="r-1" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Withdraw' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Note'), 'superseded by v2');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Withdraw release' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(bodyOf(fetchMock, `POST ${R}/withdraw`)).toEqual({ note: 'superseded by v2' });
    const summary = screen.getByRole('region', { name: 'Release' });
    expect(await within(summary).findByText('withdrawn')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run evaluation' })).not.toBeInTheDocument();
  });

  it('has no Withdraw button without packaging:release:withdraw', async () => {
    stubReleases({}, [
      ...ALL_PERMS,
      ...RELEASE_PERMS.filter((p) => p !== 'packaging:release:withdraw'),
    ]);
    renderWithClient(<ReleaseDetailView id="r-1" />);
    expect(await screen.findByRole('button', { name: 'Run evaluation' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument();
  });
});
