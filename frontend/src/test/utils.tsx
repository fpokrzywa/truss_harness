import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    ...init,
  });
}

export function problemResponse(
  status: number,
  code: string,
  extra: Record<string, unknown> = {},
): Response {
  return new Response(
    JSON.stringify({
      type: 'about:blank',
      title: `Problem ${status}`,
      status,
      code,
      request_id: 'req-123',
      ...extra,
    }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** Route-table fetch mock: key is "METHOD /path" (query string ignored). */
export type Handler = (req: { url: string; init: RequestInit }) => Response | Promise<Response>;

export function routeFetch(routes: Record<string, Handler>) {
  const calls: { key: string; url: string; init: RequestInit }[] = [];
  const fn = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const path = url.split('?')[0] as string;
    const key = `${init.method ?? 'GET'} ${path}`;
    calls.push({ key, url, init });
    const handler = routes[key];
    if (!handler) throw new Error(`Unmocked fetch: ${key}`);
    return handler({ url, init });
  };
  return Object.assign(fn, { calls });
}
