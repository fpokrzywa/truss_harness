import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Layout from '@/app/(app)/o/[org]/build/ontologies/layout';
import { SESSION, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';

vi.mock('next/navigation', () => ({ usePathname: () => '/o/acme' }));
afterEach(() => vi.unstubAllGlobals());

describe('ontology feature gate', () => {
  it('does not mount the ontology area when the feature is off', async () => {
    const fetchMock = stubApi({
      'GET /api/auth/session': () => jsonResponse({ ...SESSION, features: ['tenancy', 'build'] }),
    });
    const child = vi.fn(() => <p>ontology builder</p>);
    const Child = child;
    renderWithClient(
      <Layout>
        <Child />
      </Layout>,
    );
    expect(
      await screen.findByRole('heading', {
        name: 'This area is not available in this installation',
      }),
    ).toBeInTheDocument();
    expect(child).not.toHaveBeenCalled();
    expect(fetchMock.calls.every((c) => c.key === 'GET /api/auth/session')).toBe(true);
  });

  it('renders children when the feature is on', async () => {
    stubApi();
    renderWithClient(
      <Layout>
        <p>ontology builder</p>
      </Layout>,
    );
    expect(await screen.findByText('ontology builder')).toBeInTheDocument();
  });
});
