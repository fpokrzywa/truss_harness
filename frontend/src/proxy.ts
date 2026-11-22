import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { apiOrigin } from './lib/api-origin';

/** Header the API uses to recognise requests that came through this web proxy. The API refuses
 * token-less `/api/metrics` for such requests: every proxied request arrives from loopback. */
export const VIA_HEADER = 'x-shifde-via';

/** Canonical form of a request path for policy checks: percent-decoded (repeatedly, to defeat
 * double encoding), backslashes and repeated slashes collapsed, dot segments resolved, lower-cased.
 * Returns null when the path cannot be decoded (the caller refuses it). */
export function canonicalPath(pathname: string): string | null {
  let p = pathname;
  for (let i = 0; i < 5; i += 1) {
    let next: string;
    try {
      next = decodeURIComponent(p);
    } catch {
      return null;
    }
    if (next === p) break;
    p = next;
  }
  p = p.replace(/\\/g, '/').replace(/\/{2,}/g, '/');
  const out: string[] = [];
  for (const seg of p.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') out.pop();
    else out.push(seg);
  }
  return `/${out.join('/')}`.toLowerCase();
}

/** True for any spelling of the API's metrics endpoint, which is never served through this proxy. */
export function isMetricsPath(pathname: string): boolean {
  const c = canonicalPath(pathname);
  return c === null || c === '/api/metrics' || c.startsWith('/api/metrics/');
}

/** True for any spelling of an API path (`/api` or `/api/...`); an undecodable path counts too. */
export function isApiPath(pathname: string): boolean {
  const c = canonicalPath(pathname);
  return c === null || c === '/api' || c.startsWith('/api/');
}

/** True when `pathname` may be forwarded as it is: spelled `/api` or `/api/...` exactly, and
 * already canonical apart from one level of percent-encoding and letter case (no dot segments,
 * backslashes, repeated or trailing slashes, or double encoding). */
export function isForwardablePath(pathname: string): boolean {
  if (pathname !== '/api' && !pathname.startsWith('/api/')) return false;
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return false;
  }
  return canonicalPath(pathname) === decoded.toLowerCase();
}

/** `SHIFDE_WEB_API_PROXY`, read per request: unset, empty or `on` is on; `off` and any other value
 * is off (fail closed: production requires `off` behind the edge proxy, so a typo must not open it). */
export function apiProxyEnabled(): boolean {
  const value = (process.env.SHIFDE_WEB_API_PROXY ?? '').trim().toLowerCase();
  return value === '' || value === 'on';
}

/** The page Content-Security-Policy for `nonce` (go-live design section 5). `next dev` also needs
 * `'unsafe-eval'` (React's dev tooling); production never gets it. */
export function contentSecurityPolicy(nonce: string): string {
  const dev = process.env.NODE_ENV === 'development' ? " 'unsafe-eval'" : '';
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
  ].join('; ');
}

/** Page responses: a fresh nonce per request, handed to Next on the request CSP so it puts it on
 * its own scripts (pages render per request, see app/layout.tsx), plus the other browser headers.
 * HSTS is not set here: the edge proxy owns it (go-live design call 13). */
function pageResponse(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const csp = contentSecurityPolicy(nonce);
  const headers = new Headers(request.headers);
  headers.set('x-nonce', nonce);
  headers.set('content-security-policy', csp);
  const res = NextResponse.next({ request: { headers } });
  res.headers.set('content-security-policy', csp);
  res.headers.set('x-content-type-options', 'nosniff');
  res.headers.set('referrer-policy', 'no-referrer');
  res.headers.set('x-frame-options', 'DENY');
  res.headers.set('cross-origin-opener-policy', 'same-origin');
  return res;
}

export function proxy(request: NextRequest) {
  if (!isApiPath(request.nextUrl.pathname)) return pageResponse(request);
  if (!apiProxyEnabled()) {
    // Off in production: the edge proxy routes /api to the API, and this server forwards nothing.
    return NextResponse.json({ detail: 'not found' }, { status: 404 });
  }
  if (isMetricsPath(request.nextUrl.pathname)) {
    return NextResponse.json(
      { status: 404, title: 'Not Found', detail: 'Not Found' },
      { status: 404, headers: { 'content-type': 'application/problem+json' } },
    );
  }
  // Any other spelling of an API path (`/%61pi/x`, `//host/%252e%252e/api/x`, ...) is refused, so
  // the raw path forwarded below is exactly the path the checks above saw.
  if (!isForwardablePath(request.nextUrl.pathname)) {
    return NextResponse.json({ detail: 'not found' }, { status: 404 });
  }
  const headers = new Headers(request.headers);
  headers.set(VIA_HEADER, 'web');
  // The client chooses its own X-Forwarded-For, and Next copies it unchanged onto the rewritten
  // request (it sets the header from the socket only when it is absent, and its rewrite proxy does
  // not append). Drop it: a client must not pick the address the API limits and audits by (app
  // isolation review 2 item 1, B-2280). Next 16 does not expose the socket address to proxy.ts, so
  // the header is left unset; an edge proxy that sets X-Forwarded-For from the real address, listed
  // in SHIFDE_TRUSTED_PROXIES, is required at go-live (carry_forward, sub-project 4).
  headers.delete('x-forwarded-for');
  // Same-origin proxy so session cookies are first-party (PHASE_1_CONTRACT topology). The target
  // is resolved per request from SHIFDE_API_ORIGIN: a `rewrites()` destination in next.config.ts
  // would be frozen at `next build` time, which breaks one-build-many-installations.
  let origin: string;
  try {
    origin = apiOrigin();
  } catch (err) {
    // The raw value stays server-side: anonymous clients only learn that the proxy is misconfigured.
    console.error(
      'shifde proxy: invalid SHIFDE_API_ORIGIN:',
      err instanceof Error ? err.message : 'unknown error',
    );
    return NextResponse.json(
      {
        status: 500,
        title: 'Misconfigured',
        detail: 'The web proxy is misconfigured. Contact your administrator.',
      },
      { status: 500, headers: { 'content-type': 'application/problem+json' } },
    );
  }
  // Built from the configured origin, never by resolving the path against it: a path starting
  // with `//` would otherwise be read as a host (isolation review of #125).
  const target = new URL(origin);
  target.pathname = request.nextUrl.pathname;
  target.search = request.nextUrl.search;
  if (target.origin !== new URL(origin).origin) {
    return NextResponse.json({ detail: 'not found' }, { status: 404 });
  }
  return NextResponse.rewrite(target, { request: { headers } });
}

// Pages and /api/*; static assets skip the proxy (no CSP on them, go-live design section 5). The
// asset-extension rule does not apply under the app's route trees, so a page such as
// /o/<slug>/runs/x.js still gets its headers (#175).
export const config = {
  matcher:
    '/((?!_next/static|_next/image|favicon.ico|(?!(?:o|platform|api|login|setup)(?:/|$)).*\\.(?:png|jpg|jpeg|gif|svg|ico|webp|css|js|map|txt|woff2?)$).*)',
};
