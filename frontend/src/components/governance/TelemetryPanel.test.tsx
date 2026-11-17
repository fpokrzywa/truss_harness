import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { HealthDetails } from '@/lib/api/governance-types';
import { exporterText, TelemetryDetails } from './TelemetryPanel';

const base: HealthDetails = {
  queue: { queued: 3, leased: 1, dead_in_window: 0 },
  workers: { running: 2, known: 2 },
  telemetry: { exporter: 'none', otlp: null, buffered_spans: 12, metrics: 'prometheus text' },
};

describe('TelemetryDetails', () => {
  it('states that nothing is exported when the exporter is none', () => {
    render(<TelemetryDetails details={base} />);
    expect(screen.getByText('none')).toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent to a collector/)).toBeInTheDocument();
    expect(screen.queryByText(/OTLP spans exported/)).not.toBeInTheDocument();
  });

  it('shows OTLP export counters when the exporter is otlp', () => {
    const details: HealthDetails = {
      ...base,
      telemetry: {
        exporter: 'otlp',
        otlp: { endpoint: 'http://collector:4318', exported: 40, failed: 2 },
        buffered_spans: 0,
      },
    };
    render(<TelemetryDetails details={details} />);
    expect(screen.getByText('40 / 2')).toBeInTheDocument();
    expect(exporterText(details.telemetry)).toContain('http://collector:4318');
  });
});
