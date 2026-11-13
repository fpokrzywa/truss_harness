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

export function proxy(request: NextRequest) {
  if (isMetricsPath(request.nextUrl.pathname)) {
    return NextResponse.json(
      { status: 404, title: 'Not Found', detail: 'Not Found' },
      { status: 404, headers: { 'content-type': 'application/problem+json' } },
    );
  }
  const headers = new Headers(request.headers);
  headers.set(VIA_HEADER, 'web');
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
  const target = new URL(`${request.nextUrl.pathname}${request.nextUrl.search}`, origin);
  return NextResponse.rewrite(target, { request: { headers } });
}

export const config = {
  matcher: '/api/:path*',
};
