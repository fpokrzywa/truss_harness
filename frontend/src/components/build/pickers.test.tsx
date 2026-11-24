import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MODEL } from '@/test/appgen';
import { list, stubApi } from '@/test/security';
import { problemResponse, renderWithClient } from '@/test/utils';
import { ModelSelect } from './pickers';

afterEach(() => vi.unstubAllGlobals());

const EFFECTIVE = 'GET /api/v1/orgs/acme/models/effective';

const render = (props: { workspace?: string; project?: string; value?: string } = {}) =>
  renderWithClient(
    <ModelSelect org="acme" label="Model" value="" onChange={() => {}} {...props} />,
  );

describe('ModelSelect', () => {
  it('lists the effective models', async () => {
    stubApi({ [EFFECTIVE]: () => list([MODEL]) });
    render();
    expect(await screen.findByRole('option', { name: 'GPT X (gpt-x)' })).toBeInTheDocument();
    expect(screen.getByLabelText('Model')).toBeEnabled();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows the loading text and a disabled select while pending', async () => {
    stubApi({ [EFFECTIVE]: () => new Promise<Response>(() => {}) });
    render();
    expect(await screen.findByRole('status')).toHaveTextContent('Loading models...');
    expect(screen.getByLabelText('Model')).toBeDisabled();
  });

  it('shows an error and a disabled select when the request fails', async () => {
    stubApi({ [EFFECTIVE]: () => problemResponse(404, 'not_found') });
    render();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load models');
    expect(alert).toHaveTextContent('not_found');
    expect(screen.getByLabelText('Model')).toBeDisabled();
  });

  it('says no models are available and keeps an unknown value', async () => {
    stubApi({ [EFFECTIVE]: () => list([]) });
    render({ value: 'm-old' });
    expect(await screen.findByText(/No models are available here/)).toHaveTextContent(
      'An administrator can allow models on the Models screen.',
    );
    const select = screen.getByLabelText('Model');
    expect(select).toBeDisabled();
    expect(within(select).getByRole('option', { name: 'm-old' })).toBeInTheDocument();
  });

  it('asks for the models of the given workspace and project', async () => {
    const fetchMock = stubApi({ [EFFECTIVE]: () => list([MODEL]) });
    render({ workspace: 'eng', project: 'api' });
    await screen.findByRole('option', { name: 'GPT X (gpt-x)' });
    const url = fetchMock.calls.find((c) => c.key === EFFECTIVE)!.url;
    expect(url).toContain('workspace=eng');
    expect(url).toContain('project=api');
  });
});
