import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { artifact } from '@/test/build';
import { ALL_PERMS, SESSION, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { RunAgentLink } from './RunAgentLink';

afterEach(() => vi.unstubAllGlobals());

describe('RunAgentLink', () => {
  it('offers a run when the operate feature is advertised and agent:execute is held', async () => {
    stubApi({}, [...ALL_PERMS, 'agent:execute']);
    renderWithClient(<RunAgentLink org="acme" artifact={artifact()} />);
    expect(await screen.findByRole('region', { name: 'Run this agent' })).toBeInTheDocument();
  });

  it('is hidden while the backend does not advertise the operate feature', async () => {
    const fetchMock = stubApi(
      {
        'GET /api/auth/session': () =>
          jsonResponse({ ...SESSION, features: SESSION.features.filter((f) => f !== 'operate') }),
      },
      [...ALL_PERMS, 'agent:execute'],
    );
    const { container } = renderWithClient(<RunAgentLink org="acme" artifact={artifact()} />);
    await vi.waitFor(() =>
      expect(fetchMock.calls.some((c) => c.key === 'GET /api/auth/session')).toBe(true),
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();
  });
});
