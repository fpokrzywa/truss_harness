import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PlatformInstallation } from '@/lib/api/types';
import { jsonResponse, problemResponse, renderWithClient, routeFetch } from '@/test/utils';
import { PlatformSettingsContent, PlatformSettingsView } from './PlatformSettingsView';

const data: PlatformInstallation = {
  version: '0.9.0',
  env: 'development',
  deployment_mode: 'isolated',
  migration: { current: 'p09_0001_x', expected_head: 'p09_0001_x', ok: true },
  feature_flags: { pipelines: true, ontology: false },
  auth: {
    local_enabled: true,
    oidc_providers: [{ name: 'Corporate IdP', issuer_host: 'login.example.test' }],
  },
  security: {
    session_idle_minutes: 30,
    session_absolute_hours: 12,
    api_key_max_days: 90,
    egress_default_allow_private: false,
  },
  limits: { max_run_seconds: 900, max_tool_calls: 50, inline_output_max_bytes: 65536 },
  workers: null,
};

afterEach(() => vi.unstubAllGlobals());

describe('PlatformSettingsContent', () => {
  it('shows the read-only note and the reported values', () => {
    const { container } = renderWithClient(<PlatformSettingsContent data={data} />);
    // Dropped by the unified backend (B-1101, B-1102): no profile text, no note.
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(screen.getByText('0.9.0')).toBeInTheDocument();
    expect(screen.getByText('Migrations up to date').nextElementSibling).toHaveTextContent('OK');
    expect(screen.getByText('pipelines').nextElementSibling).toHaveTextContent('Enabled');
    expect(screen.getByText('ontology').nextElementSibling).toHaveTextContent('Disabled');
    expect(screen.getByText('OIDC provider: Corporate IdP').nextElementSibling).toHaveTextContent(
      'login.example.test',
    );
    expect(screen.getByText('Session idle timeout').nextElementSibling).toHaveTextContent('30 min');
    expect(screen.getByText('Maximum tool calls per run').nextElementSibling).toHaveTextContent(
      '50',
    );
    // No form controls: the page is read-only.
    expect(container.querySelector('input,button,select,textarea')).toBeNull();
  });

  it('reports unreported workers and shows none of the dropped profile sections', () => {
    renderWithClient(<PlatformSettingsContent data={data} />);
    expect(screen.getByText('Worker heartbeats').nextElementSibling).toHaveTextContent(
      'not reported',
    );
    for (const title of ['Installation profile', 'Data retention', 'Governance defaults']) {
      expect(screen.queryByRole('region', { name: title })).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/Recorded only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/installation profile/i)).not.toBeInTheDocument();
  });

  it('flags a migration mismatch and tolerates absent sections', () => {
    renderWithClient(
      <PlatformSettingsContent
        data={{
          version: '1',
          env: 'production',
          deployment_mode: 'shared',
          migration: { current: null, expected_head: 'h', ok: false },
        }}
      />,
    );
    expect(screen.getByText('Mismatch')).toBeInTheDocument();
    expect(screen.getByText('Session idle timeout').nextElementSibling).toHaveTextContent(
      'not reported',
    );
  });

  it('lists reporting workers', () => {
    renderWithClient(
      <PlatformSettingsContent
        data={{
          ...data,
          workers: [{ name: 'w1', last_seen_at: '2026-11-05T10:00:00Z', status: 'stale' }],
        }}
      />,
    );
    expect(screen.getByText('w1').nextElementSibling).toHaveTextContent(/Stale/);
  });
});

describe('PlatformSettingsView', () => {
  it('loads from the installation endpoint', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch({ 'GET /api/v1/platform/installation': () => jsonResponse(data) }),
    );
    renderWithClient(<PlatformSettingsView />);
    expect(await screen.findByRole('heading', { name: 'Platform Settings' })).toBeInTheDocument();
    expect(screen.getByText('Corporate IdP', { exact: false })).toBeInTheDocument();
  });

  it('shows a friendly administrators-only state on 403', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch({
        'GET /api/v1/platform/installation': () => problemResponse(403, 'forbidden'),
      }),
    );
    renderWithClient(<PlatformSettingsView />);
    expect(await screen.findByText(/visible to platform administrators only/)).toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });

  it('shows a structured error for server failures', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch({
        'GET /api/v1/platform/installation': () => problemResponse(500, 'internal'),
      }),
    );
    renderWithClient(<PlatformSettingsView />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
