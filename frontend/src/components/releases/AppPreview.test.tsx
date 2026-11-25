import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SNOWFLAKE_TARGET_KIND, TARGET_KIND } from '@/lib/api/packaging-types';
import { APP_PREVIEW, ORG } from '@/test/releases';
import { stubApi } from '@/test/security';
import { jsonResponse, problemResponse, renderWithClient, type Handler } from '@/test/utils';
import { AppPreview } from './AppPreview';

afterEach(() => vi.unstubAllGlobals());

const PREVIEW = `GET ${ORG}/releases/r-2/app-preview`;

const NOTE = 'Deployed as Streamlit in Snowflake; layout differs';

const show = (handler: Handler, targetKind: unknown = TARGET_KIND) => {
  const fetchMock = stubApi({ [PREVIEW]: handler });
  renderWithClient(<AppPreview org="acme" releaseId="r-2" targetKind={targetKind} />);
  return fetchMock;
};

describe('AppPreview', () => {
  it('shows a status line while the preview loads', async () => {
    show(() => new Promise<Response>(() => {}));
    expect(await screen.findByRole('status')).toHaveTextContent('Loading app preview...');
  });

  it('shows release_source_changed in plain words and no screens', async () => {
    show(() =>
      problemResponse(409, 'release_source_changed', { detail: 'a version the release pins' }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'A version this release pins has changed or cannot be read; seal the release again.',
    );
    expect(screen.queryByRole('navigation', { name: 'Screens' })).not.toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Files' })).not.toBeInTheDocument();
  });

  it('shows release_has_no_app in plain words', async () => {
    show(() => problemResponse(409, 'release_has_no_app'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This release has no generated app.',
    );
    expect(screen.queryByRole('table', { name: 'Files' })).not.toBeInTheDocument();
  });

  it('shows release_pins_hidden in plain words', async () => {
    show(() => problemResponse(409, 'release_pins_hidden'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This release pins a version you cannot see, so its app cannot be previewed.',
    );
    expect(screen.queryByRole('table', { name: 'Files' })).not.toBeInTheDocument();
  });

  it('shows the detail of any other 409', async () => {
    show(() =>
      problemResponse(409, 'generator_version_unsupported', {
        detail: 'the release was sealed for another app generator version; seal it again',
      }),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'the release was sealed for another app generator version; seal it again',
    );
  });

  it('lists the files and draws the screens over sample rows without another request', async () => {
    const fetchMock = show(() => jsonResponse(APP_PREVIEW));
    const files = await screen.findByRole('table', { name: 'Files' });
    for (const f of APP_PREVIEW.files) {
      expect(within(files).getByText(f.path).closest('tr')).toHaveTextContent(f.sha256);
    }
    expect(screen.getByText('shifde.appgen/1.0')).toBeInTheDocument();
    expect(screen.getByText(/sample data; nothing is sent/)).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Screens' });
    // embedded in the page's own main landmark: the renderer adds none
    expect(screen.queryByRole('main')).not.toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: 'Ask about sales' })).toBeInTheDocument();
    const before = fetchMock.calls.length;
    await userEvent.click(within(nav).getByRole('button', { name: 'Orders by region' }));
    await userEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('3 rows')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'region' })).toBeInTheDocument();
    expect(screen.getByText('sample 1.1')).toBeInTheDocument();
    // the chat answers in place, too
    await userEvent.click(within(nav).getByRole('button', { name: 'Ask about sales' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Message' }), 'hello');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText(/This is a preview/)).toBeInTheDocument();
    expect(fetchMock.calls.length).toBe(before);
    expect(fetchMock.calls.filter((c) => c.key === PREVIEW)).toHaveLength(1);
  });

  it('notes the Streamlit layout of a Snowflake release above its screens', async () => {
    show(() => jsonResponse(APP_PREVIEW), SNOWFLAKE_TARGET_KIND);
    const note = await screen.findByText(NOTE);
    const nav = screen.getByRole('navigation', { name: 'Screens' });
    expect(note.compareDocumentPosition(nav)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText(/sample data; nothing is sent/)).toBeInTheDocument();
  });

  it.each([TARGET_KIND, 'other.app/v1', undefined])(
    'has no note for target kind %s',
    async (kind) => {
      show(() => jsonResponse(APP_PREVIEW), kind);
      expect(await screen.findByRole('navigation', { name: 'Screens' })).toBeInTheDocument();
      expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
    },
  );

  it('has no note on a 409 of a Snowflake release', async () => {
    show(() => problemResponse(409, 'release_source_changed'), SNOWFLAKE_TARGET_KIND);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(NOTE)).not.toBeInTheDocument();
  });

  it('shows an empty state when the screens cannot be parsed', async () => {
    show(() => jsonResponse({ ...APP_PREVIEW, screens: [{ kind: 'bogus' }] }));
    expect(
      await screen.findByText('The screens of this release cannot be shown.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Screens' })).not.toBeInTheDocument();
  });
});
