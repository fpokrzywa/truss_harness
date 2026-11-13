import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SESSION, stubApi } from '@/test/security';
import { jsonResponse, renderWithClient } from '@/test/utils';
import { FeatureGate } from './FeatureGate';
import { Sidebar } from './Sidebar';

vi.mock('next/navigation', () => ({ usePathname: () => '/o/acme' }));
afterEach(() => vi.unstubAllGlobals());

const session = (features?: string[]) => {
  const { features: _drop, ...rest } = SESSION;
  void _drop;
  return features ? { ...rest, features } : rest;
};

describe('FeatureGate', () => {
  it('renders children when the feature is enabled', async () => {
    stubApi({ 'GET /api/auth/session': () => jsonResponse(session(['tenancy', 'build'])) });
    renderWithClient(
      <FeatureGate feature="build">
        <p>builder</p>
      </FeatureGate>,
    );
    expect(await screen.findByText('builder')).toBeInTheDocument();
  });

  it('shows the neutral page and does not mount children when disabled', async () => {
    const fetchMock = stubApi({
      'GET /api/auth/session': () => jsonResponse(session(['tenancy'])),
    });
    const child = vi.fn(() => <p>builder</p>);
    const Child = child;
    renderWithClient(
      <FeatureGate feature="build">
        <Child />
      </FeatureGate>,
    );
    expect(
      await screen.findByRole('heading', {
        name: 'This area is not available in this installation',
      }),
    ).toBeInTheDocument();
    expect(child).not.toHaveBeenCalled();
    expect(fetchMock.calls.every((c) => c.key === 'GET /api/auth/session')).toBe(true);
  });

  it('treats missing features as tenancy only', async () => {
    stubApi({ 'GET /api/auth/session': () => jsonResponse(session()) });
    renderWithClient(
      <FeatureGate feature="operate">
        <p>runs</p>
      </FeatureGate>,
    );
    expect(
      await screen.findByText('This area is not available in this installation'),
    ).toBeInTheDocument();
    expect(screen.queryByText('runs')).not.toBeInTheDocument();
  });
});

describe('Sidebar feature filtering', () => {
  const perms = ['member:read', 'prompt:read', 'run:read', 'approval:read'];

  it('hides Build and Operate when only tenancy and security are enabled', async () => {
    stubApi(
      { 'GET /api/auth/session': () => jsonResponse(session(['tenancy', 'security'])) },
      perms,
    );
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    await screen.findByRole('link', { name: 'Users', hidden: true });
    for (const h of ['Build', 'Operate', 'Integrate'])
      expect(screen.queryByRole('heading', { name: h })).not.toBeInTheDocument();
  });

  it('shows Operate links when enabled and permitted', async () => {
    stubApi(
      { 'GET /api/auth/session': () => jsonResponse(session(['tenancy', 'operate'])) },
      perms,
    );
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(await screen.findByRole('link', { name: 'Runs', hidden: true })).toHaveAttribute(
      'href',
      '/o/acme/runs',
    );
    expect(screen.getByRole('link', { name: 'Approvals', hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Users', hidden: true })).not.toBeInTheDocument();
  });

  it('falls back to tenancy items when the session has no features', async () => {
    stubApi({ 'GET /api/auth/session': () => jsonResponse(session()) }, perms);
    renderWithClient(<Sidebar isPlatformAdmin={false} />);
    expect(
      await screen.findByRole('link', { name: 'Workspaces', hidden: true }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Runs', hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Prompts', hidden: true })).not.toBeInTheDocument();
  });

  it('gates the pipelines area independently of build', async () => {
    const fetchMock = stubApi({
      'GET /api/auth/session': () => jsonResponse(session(['tenancy', 'build'])),
    });
    renderWithClient(
      <FeatureGate feature="pipelines">
        <p>pipeline builder</p>
      </FeatureGate>,
    );
    expect(
      await screen.findByRole('heading', {
        name: 'This area is not available in this installation',
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText('pipeline builder')).not.toBeInTheDocument();
    expect(fetchMock.calls.every((c) => c.key === 'GET /api/auth/session')).toBe(true);
  });

  it('renders pipelines when the feature is enabled', async () => {
    stubApi({
      'GET /api/auth/session': () => jsonResponse(session(['tenancy', 'build', 'pipelines'])),
    });
    renderWithClient(
      <FeatureGate feature="pipelines">
        <p>pipeline builder</p>
      </FeatureGate>,
    );
    expect(await screen.findByText('pipeline builder')).toBeInTheDocument();
  });
});
