import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { REQ, REQ_ID, request, stubAssist } from '@/test/assist';
import { jsonResponse } from '@/test/utils';
import type { AssistRequestOut } from './api/assist-types';
import type { RunStatus } from './api/operate-types';
import { ASSIST_POLL_LIMIT_MS, assistPollInterval, useAssistRequest } from './assist-hooks';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const withRun = (status: RunStatus): Partial<AssistRequestOut> => ({
  status: 'routed',
  run: { id: 'run-1', status, error_code: null },
});

describe('assistPollInterval', () => {
  it.each(['queued', 'classifying', 'handing_over'] as const)('polls every 3 s in %s', (status) => {
    expect(assistPollInterval(request({ status, run: null }), false)).toBe(3000);
  });

  it.each(['pending', 'running', 'waiting_for_approval'] as const)(
    'polls every 3 s while the run is %s',
    (status) => expect(assistPollInterval(request(withRun(status)), false)).toBe(3000),
  );

  it.each(['completed', 'failed', 'cancelled', 'timed_out'] as const)(
    'stops once the run is %s',
    (status) => expect(assistPollInterval(request(withRun(status)), false)).toBe(false),
  );

  it('stops without a run, when refused or failed, and before the first answer', () => {
    expect(assistPollInterval(request({ run: null }), false)).toBe(false);
    for (const status of ['refused', 'failed'] as const)
      expect(assistPollInterval(request({ status, run: null }), false)).toBe(false);
    expect(assistPollInterval(undefined, false)).toBe(false);
  });

  it('stops once the time limit is reached', () => {
    expect(assistPollInterval(request(), true)).toBe(false);
  });
});

describe('useAssistRequest', () => {
  it('stops polling 10 minutes after it started', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = stubAssist({ [`GET ${REQ}`]: () => jsonResponse(request()) });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useAssistRequest('acme', REQ_ID), { wrapper });
    await waitFor(() => expect(result.current.polling).toBe(true));
    const gets = () => fetchMock.calls.filter((c) => c.key === `GET ${REQ}`).length;

    await act(() => vi.advanceTimersByTimeAsync(ASSIST_POLL_LIMIT_MS - 30_000));
    expect(result.current).toMatchObject({ polling: true, timedOut: false });
    expect(gets()).toBeGreaterThan(150);

    await act(() => vi.advanceTimersByTimeAsync(31_000));
    expect(result.current).toMatchObject({ polling: false, timedOut: true });
    const stopped = gets();
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(gets()).toBe(stopped);
  });
});
