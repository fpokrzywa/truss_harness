import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { PlatformStatus } from '@/lib/api/types';
import { PlatformStatusContent } from './PlatformStatusView';

const base: PlatformStatus = {
  version: '0.5.0',
  environment: 'test',
  database: { status: 'ok', revision: 'a', head: 'a' },
  auth: { local_enabled: true, oidc_providers: 0 },
  worker: { status: 'healthy', count: 2, last_heartbeat_at: '2026-11-03T10:00:00Z' },
};
const ready = { httpStatus: 200, body: { status: 'ready' } };

describe('PlatformStatusContent worker health', () => {
  it('shows status, count and last heartbeat', () => {
    render(<PlatformStatusContent status={base} ready={ready} />);
    expect(screen.getByText('Healthy')).toBeInTheDocument();
    expect(screen.getByText('Workers reporting').nextElementSibling).toHaveTextContent('2');
    expect(
      screen.getByText('Last worker heartbeat').nextElementSibling?.querySelector('time'),
    ).toHaveAttribute('datetime', '2026-11-03T10:00:00Z');
  });

  it('shows a stopped worker without a heartbeat', () => {
    render(
      <PlatformStatusContent
        status={{ ...base, worker: { status: 'not_running', count: 0, last_heartbeat_at: null } }}
        ready={ready}
      />,
    );
    expect(screen.getByText('Not running')).toBeInTheDocument();
    expect(screen.getByText('Never')).toBeInTheDocument();
  });
});

describe('PlatformStatusContent installation details', () => {
  it('shows migration revision and deployment mode from the installation endpoint', () => {
    render(
      <PlatformStatusContent
        status={base}
        ready={ready}
        installation={{
          version: '0.9.0',
          env: 'test',
          deployment_mode: 'isolated',
          migration: { current: 'rev_9', expected_head: 'rev_9', ok: true },
        }}
      />,
    );
    expect(screen.getByText('Version').nextElementSibling).toHaveTextContent('0.9.0');
    expect(screen.getByText('Migration revision').nextElementSibling).toHaveTextContent('rev_9');
    expect(screen.getByText('Deployment mode').nextElementSibling).toHaveTextContent('isolated');
  });

  it('omits the extra rows when the endpoint was not accessible', () => {
    render(<PlatformStatusContent status={base} ready={ready} />);
    expect(screen.getByText('Version').nextElementSibling).toHaveTextContent('0.5.0');
    expect(screen.queryByText('Migration revision')).not.toBeInTheDocument();
  });
});
