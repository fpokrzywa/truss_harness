import { describe, expect, it, vi } from 'vitest';
import { jsonResponse, problemResponse, routeFetch } from '@/test/utils';
import { ApiClient } from './client';
import { createEndpoints } from './endpoints';
import { ApiError, NetworkError } from './errors';

const session = {
  user: { id: 'u1', email: 'a@b.c', display_name: 'A', is_platform_admin: false },
  csrf_token: 'csrf-abc',
  memberships: [],
  expires_at: '2030-01-01T00:00:00Z',
};

function setup(routes: Parameters<typeof routeFetch>[0]) {
  const fetchMock = routeFetch({ 'GET /api/auth/session': () => jsonResponse(session), ...routes });
  const onUnauthorized = vi.fn();
  const client = new ApiClient({ fetchImpl: fetchMock as unknown as typeof fetch, onUnauthorized });
  return { fetchMock, onUnauthorized, client };
}

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string>)[name];
}

describe('ApiClient', () => {
  it('sends credentials and no CSRF header on GET', async () => {
    const { client, fetchMock } = setup({ 'GET /api/v1/orgs': () => jsonResponse({ items: [] }) });
    await client.request('/api/v1/orgs');
    const call = fetchMock.calls.find((c) => c.key === 'GET /api/v1/orgs');
    expect(call?.init.credentials).toBe('include');
    expect(headerOf(call?.init ?? {}, 'X-CSRF-Token')).toBeUndefined();
  });

  it('attaches X-CSRF-Token from the session on mutations', async () => {
    const { client, fetchMock } = setup({
      'POST /api/v1/orgs': () => jsonResponse({ id: 'o1' }, { status: 201 }),
    });
    await client.request('/api/v1/orgs', { method: 'POST', body: { slug: 'a', name: 'A' } });
    const post = fetchMock.calls.find((c) => c.key === 'POST /api/v1/orgs');
    expect(headerOf(post?.init ?? {}, 'X-CSRF-Token')).toBe('csrf-abc');
    expect(headerOf(post?.init ?? {}, 'Content-Type')).toBe('application/json');
    expect(post?.init.body).toBe(JSON.stringify({ slug: 'a', name: 'A' }));
    // Session is fetched once and cached for later mutations.
    await client.request('/api/v1/orgs', { method: 'POST', body: {} });
    expect(fetchMock.calls.filter((c) => c.key === 'GET /api/auth/session')).toHaveLength(1);
  });

  it('skips CSRF for login and caches the token from the login session', async () => {
    const { fetchMock, client } = setup({
      'POST /api/auth/local/login': () => jsonResponse({ ...session, csrf_token: 'fresh' }),
      'POST /api/v1/orgs': () => jsonResponse({ id: 'o1' }),
    });
    const api = createEndpoints(client);
    await api.login('a@b.c', 'pw');
    expect(fetchMock.calls.some((c) => c.key === 'GET /api/auth/session')).toBe(false);
    await api.createOrg({ slug: 'a', name: 'A' });
    const post = fetchMock.calls.find((c) => c.key === 'POST /api/v1/orgs');
    expect(headerOf(post?.init ?? {}, 'X-CSRF-Token')).toBe('fresh');
  });

  it('parses problem+json into a typed ApiError with request_id and field errors', async () => {
    const { client } = setup({
      'GET /api/v1/orgs': () =>
        problemResponse(422, 'validation_error', {
          detail: 'Bad input',
          errors: [{ loc: ['body', 'name'], msg: 'required', type: 'missing' }],
        }),
    });
    const err = await client.request('/api/v1/orgs').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(422);
    expect(apiErr.code).toBe('validation_error');
    expect(apiErr.detail).toBe('Bad input');
    expect(apiErr.requestId).toBe('req-123');
    expect(apiErr.fieldErrors).toEqual([
      { loc: ['body', 'name'], msg: 'required', type: 'missing' },
    ]);
  });

  it('falls back to X-Request-ID and status for non-problem error bodies', async () => {
    const { client } = setup({
      'GET /api/v1/orgs': () =>
        new Response('<html>Bad gateway</html>', {
          status: 502,
          headers: { 'X-Request-ID': 'hdr-1' },
        }),
    });
    const err = (await client.request('/api/v1/orgs').catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.requestId).toBe('hdr-1');
  });

  it('redirects on 401 and still throws', async () => {
    const { client, onUnauthorized } = setup({
      'GET /api/v1/orgs': () => problemResponse(401, 'unauthenticated'),
    });
    await expect(client.request('/api/v1/orgs')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it('does not redirect on 401 when disabled (login, session probe)', async () => {
    const { client, onUnauthorized } = setup({
      'POST /api/auth/local/login': () => problemResponse(401, 'invalid_credentials'),
    });
    await expect(
      client.request('/api/auth/local/login', {
        method: 'POST',
        body: {},
        csrf: false,
        redirectOnUnauthorized: false,
      }),
    ).rejects.toMatchObject({ code: 'invalid_credentials' });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('refreshes the CSRF token once and retries on csrf_failed', async () => {
    let attempts = 0;
    let sessions = 0;
    const { client, fetchMock } = setup({
      'GET /api/auth/session': () =>
        jsonResponse({ ...session, csrf_token: sessions++ === 0 ? 'stale' : 'good' }),
      'POST /api/v1/orgs': ({ init }) => {
        attempts++;
        return headerOf(init, 'X-CSRF-Token') === 'good'
          ? jsonResponse({ id: 'o1' })
          : problemResponse(403, 'csrf_failed');
      },
    });
    await expect(client.request('/api/v1/orgs', { method: 'POST', body: {} })).resolves.toEqual({
      id: 'o1',
    });
    expect(attempts).toBe(2);
    expect(fetchMock.calls.filter((c) => c.key === 'GET /api/auth/session')).toHaveLength(2);
  });

  it('wraps network failures', async () => {
    const client = new ApiClient({
      fetchImpl: (() => Promise.reject(new TypeError('boom'))) as unknown as typeof fetch,
      onUnauthorized: vi.fn(),
    });
    await expect(client.request('/api/v1/orgs')).rejects.toBeInstanceOf(NetworkError);
  });

  it('returns undefined for 204 and tolerates 503 on getTolerant', async () => {
    const { client } = setup({
      'POST /api/auth/logout': () => new Response(null, { status: 204 }),
      'GET /api/ready': () =>
        jsonResponse({ status: 'not_ready', checks: { database: 'fail' } }, { status: 503 }),
    });
    await expect(client.request('/api/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
    await expect(client.getTolerant('/api/ready')).resolves.toEqual({
      status: 503,
      body: { status: 'not_ready', checks: { database: 'fail' } },
    });
  });

  it('builds query strings, dropping empty values', async () => {
    const { client, fetchMock } = setup({
      'GET /api/v1/orgs/acme/dashboard': () => jsonResponse({}),
    });
    await createEndpoints(client).dashboard('acme', 'ws1', undefined);
    expect(fetchMock.calls.at(-1)?.url).toBe('/api/v1/orgs/acme/dashboard?workspace=ws1');
  });
});

describe('list endpoint amendments (A1, A2)', () => {
  it('requests scope=all for platform org listing and include_archived only when asked', async () => {
    const { client, fetchMock } = setup({
      'GET /api/v1/orgs': () => jsonResponse({ items: [], next_cursor: null }),
      'GET /api/v1/orgs/a/workspaces': () => jsonResponse({ items: [], next_cursor: null }),
    });
    const api = createEndpoints(client);
    await api.listOrgs(true);
    await api.listOrgs();
    await api.listWorkspaces('a');
    await api.listWorkspaces('a', true);
    const urls = fetchMock.calls.filter((c) => !c.key.includes('session')).map((c) => c.url);
    expect(urls).toEqual([
      '/api/v1/orgs?limit=200&scope=all',
      '/api/v1/orgs?limit=200',
      '/api/v1/orgs/a/workspaces?limit=200',
      '/api/v1/orgs/a/workspaces?limit=200&include_archived=true',
    ]);
  });
});
