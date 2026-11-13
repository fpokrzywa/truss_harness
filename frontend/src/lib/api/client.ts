import { ApiError, NetworkError, parseProblem } from './errors';
import type { Session } from './types';

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  /** Redirect to /login on 401 (default true). Disabled for login and session probes. */
  redirectOnUnauthorized?: boolean;
  signal?: AbortSignal;
  /** Attach X-CSRF-Token on mutations (default true). Login has no session yet. */
  csrf?: boolean;
  /** Extra request headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>;
}

export interface ApiClientOptions {
  fetchImpl?: typeof fetch;
  /** Called on 401 (when redirecting is enabled). Default navigates the browser to /login. */
  onUnauthorized?: () => void;
}

const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export function loginRedirectUrl(here: string): string {
  return here === '/' || here.startsWith('/login')
    ? '/login'
    : `/login?next=${encodeURIComponent(here)}`;
}

function defaultOnUnauthorized(): void {
  if (typeof window === 'undefined' || window.location.pathname.startsWith('/login')) return;
  window.location.assign(loginRedirectUrl(`${window.location.pathname}${window.location.search}`));
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '') params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export class ApiClient {
  private csrfToken: string | undefined;
  private readonly fetchImpl: typeof fetch;
  private readonly onUnauthorized: () => void;

  constructor(opts: ApiClientOptions = {}) {
    // Wrapped so `fetch` is always invoked unbound (illegal-invocation guard in browsers).
    this.fetchImpl = opts.fetchImpl ?? ((input, init) => fetch(input, init));
    this.onUnauthorized = opts.onUnauthorized ?? defaultOnUnauthorized;
  }

  setCsrfToken(token: string | undefined): void {
    this.csrfToken = token;
  }

  /** Fetch the session (and cache its CSRF token). Does not redirect on 401. */
  async getSession(): Promise<Session> {
    const session = await this.request<Session>('/api/auth/session', {
      redirectOnUnauthorized: false,
    });
    this.csrfToken = session.csrf_token;
    return session;
  }

  async request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const res = await this.send(path, opts, true);
    if (res.status === 204) return undefined as T;
    try {
      return (await res.json()) as T;
    } catch {
      throw new ApiError({
        status: res.status,
        title: 'Invalid response',
        detail: 'The server returned a response that was not valid JSON.',
        requestId: res.headers.get('X-Request-ID') ?? undefined,
      });
    }
  }

  /**
   * GET without throwing on non-2xx (used by /api/ready, which reports failing
   * checks in a 503 body). Network errors still throw.
   */
  async getTolerant<T>(path: string): Promise<{ status: number; body: T | undefined }> {
    let res: Response;
    try {
      res = await this.fetchImpl(path, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
    } catch (e) {
      throw new NetworkError(e);
    }
    let body: T | undefined;
    try {
      body = (await res.json()) as T;
    } catch {
      body = undefined; // non-JSON body; caller treats the check results as unknown
    }
    return { status: res.status, body };
  }

  private async send(
    path: string,
    opts: RequestOptions,
    allowCsrfRetry: boolean,
  ): Promise<Response> {
    const method = opts.method ?? 'GET';
    const headers: Record<string, string> = { Accept: 'application/json', ...(opts.headers ?? {}) };
    const init: RequestInit = { method, credentials: 'include', headers, signal: opts.signal };
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (MUTATING.has(method) && opts.csrf !== false) {
      if (this.csrfToken === undefined) {
        try {
          await this.getSession();
        } catch (e) {
          if (e instanceof ApiError && e.status === 401 && opts.redirectOnUnauthorized !== false) {
            this.onUnauthorized();
          }
          throw e;
        }
      }
      if (this.csrfToken !== undefined) headers['X-CSRF-Token'] = this.csrfToken;
    }

    let res: Response;
    try {
      res = await this.fetchImpl(buildUrl(path, opts.query), init);
    } catch (e) {
      throw new NetworkError(e);
    }
    if (res.ok) return res;

    const problem = await parseProblem(res);
    if (problem.status === 401 && opts.redirectOnUnauthorized !== false) {
      this.csrfToken = undefined;
      this.onUnauthorized();
    }
    if (problem.code === 'csrf_failed' && allowCsrfRetry && MUTATING.has(method)) {
      // Token may have rotated; refresh once and retry.
      this.csrfToken = undefined;
      await this.getSession();
      return this.send(path, opts, false);
    }
    throw problem;
  }
}

/** App-wide singleton used by hooks. Tests construct their own ApiClient. */
export const api = new ApiClient();
