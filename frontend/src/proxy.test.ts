import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VIA_HEADER, canonicalPath, isMetricsPath, proxy } from './proxy';
// Page headers, SHIFDE_WEB_API_PROXY and the matcher (go-live design section 5).
import { beforeAll } from 'vitest';
import { config, contentSecurityPolicy } from './proxy';

describe('web proxy metrics exclusion', () => {
  it.each([
    '/api/metrics',
    '/api/metrics/',
    '/api/Metrics',
    '/api/%6detrics',
    '/api/%256detrics',
    '/api//metrics',
    '/api/x/../metrics',
    '/api/./metrics',
    '/api\\metrics',
    '/api/metrics/extra',
    '/api/%E0%A4%A', // undecodable -> refused
  ])('refuses %s', (p) => {
    expect(isMetricsPath(p)).toBe(true);
  });

  it.each(['/api/health', '/api/v1/orgs/demo-a/usage/summary', '/api/metricsx', '/api/v1/metrics'])(
    'passes %s',
    (p) => {
      expect(isMetricsPath(p)).toBe(false);
    },
  );

  it('canonicalises paths', () => {
    expect(canonicalPath('/API//V1/./orgs/../x')).toBe('/api/v1/x');
  });
});

describe('proxy()', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('answers 404 for an encoded metrics path without forwarding it', () => {
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/%6detrics'));
    expect(res.status).toBe(404);
    expect(res.headers.get('x-middleware-next')).toBeNull();
  });

  it('proxies other API requests to SHIFDE_API_ORIGIN, tagged with the via header (client value replaced)', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'https://api.example.test:8443');
    const req = new NextRequest('http://127.0.0.1:3710/api/health?x=1', {
      headers: { [VIA_HEADER]: 'spoofed' },
    });
    const res = proxy(req);
    expect(res.headers.get('x-middleware-rewrite')).toBe(
      'https://api.example.test:8443/api/health?x=1',
    );
    expect(res.headers.get(`x-middleware-request-${VIA_HEADER}`)).toBe('web');
  });

  it('forwards client headers (cookie, CSRF, origin) and cannot have the via header spoofed or removed', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'https://api.example.test:8443');
    const res = proxy(
      new NextRequest('http://127.0.0.1:3710/api/v1/x', {
        method: 'POST',
        headers: {
          cookie: 'shifde_session=abc',
          'x-csrf-token': 'tok',
          origin: 'http://127.0.0.1:3710',
          authorization: 'Bearer k',
          'x-request-id': 'r1',
          [VIA_HEADER]: 'spoofed',
        },
      }),
    );
    const fwd = (n: string) => res.headers.get(`x-middleware-request-${n}`);
    expect(fwd('cookie')).toBe('shifde_session=abc');
    expect(fwd('x-csrf-token')).toBe('tok');
    expect(fwd('origin')).toBe('http://127.0.0.1:3710');
    expect(fwd('authorization')).toBe('Bearer k');
    expect(fwd('x-request-id')).toBe('r1');
    expect(fwd(VIA_HEADER)).toBe('web');
    // Without a client-sent via header it is still added.
    const bare = proxy(new NextRequest('http://127.0.0.1:3710/api/v1/x'));
    expect(bare.headers.get(`x-middleware-request-${VIA_HEADER}`)).toBe('web');
  });

  it('does not forward the inbound X-Forwarded-For (a client cannot choose the address the API uses)', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'https://api.example.test:8443');
    const res = proxy(
      new NextRequest('http://127.0.0.1:3710/api/auth/local/login', {
        method: 'POST',
        headers: { 'x-forwarded-for': '127.0.0.1', cookie: 'a=b' },
      }),
    );
    expect(res.headers.get('x-middleware-request-x-forwarded-for')).toBeNull();
    expect(res.headers.get('x-middleware-override-headers') ?? '').not.toContain('x-forwarded-for');
    expect(res.headers.get('x-middleware-request-cookie')).toBe('a=b');
  });

  it('does not leak the invalid origin value in the 500 body', async () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'ftp://secret-host.internal');
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    const body = await res.text();
    expect(res.status).toBe(500);
    expect(body).not.toContain('secret-host');
    expect(body).not.toContain('SHIFDE_API_ORIGIN');
    spy.mockRestore();
  });

  it('reads the origin at request time, not at import time', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    const a = proxy(new NextRequest('http://127.0.0.1:3710/api/ready'));
    expect(a.headers.get('x-middleware-rewrite')).toBe('http://10.0.0.5:9000/api/ready');
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.6:9000');
    const b = proxy(new NextRequest('http://127.0.0.1:3710/api/ready'));
    expect(b.headers.get('x-middleware-rewrite')).toBe('http://10.0.0.6:9000/api/ready');
  });

  it('answers 500 problem+json for an invalid SHIFDE_API_ORIGIN', async () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'not a url');
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(res.status).toBe(500);
    expect(res.headers.get('content-type')).toBe('application/problem+json');
  });

  it('still refuses metrics when an origin is configured', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/metrics'));
    expect(res.status).toBe(404);
    expect(res.headers.get('x-middleware-rewrite')).toBeNull();
  });
});

const PROD_CSP = (n: string) =>
  `default-src 'self'; script-src 'self' 'nonce-${n}' 'strict-dynamic'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`;

function nonceOf(csp: string | null): string {
  const m = /'nonce-([^']+)'/.exec(csp ?? '');
  if (!m?.[1]) throw new Error(`no nonce in ${csp}`);
  return m[1];
}

describe('page security headers', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('sets the production CSP and the other browser headers on a page response', () => {
    vi.stubEnv('NODE_ENV', 'production');
    const res = proxy(new NextRequest('http://127.0.0.1:3710/login'));
    expect(res.headers.get('x-middleware-next')).toBe('1');
    expect(res.headers.get('x-middleware-rewrite')).toBeNull();
    const csp = res.headers.get('content-security-policy');
    expect(csp).toBe(PROD_CSP(nonceOf(csp)));
    expect(csp).not.toContain('unsafe-eval');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('referrer-policy')).toBe('no-referrer');
    expect(res.headers.get('x-frame-options')).toBe('DENY');
    expect(res.headers.get('cross-origin-opener-policy')).toBe('same-origin');
  });

  it("adds 'unsafe-eval' to script-src only in development", () => {
    vi.stubEnv('NODE_ENV', 'development');
    expect(contentSecurityPolicy('abc')).toBe(
      PROD_CSP('abc').replace("'strict-dynamic'", "'strict-dynamic' 'unsafe-eval'"),
    );
    const res = proxy(new NextRequest('http://127.0.0.1:3710/o/demo-a/command'));
    const csp = res.headers.get('content-security-policy');
    expect(csp).toBe(
      PROD_CSP(nonceOf(csp)).replace("'strict-dynamic'", "'strict-dynamic' 'unsafe-eval'"),
    );
    vi.stubEnv('NODE_ENV', 'production');
    expect(contentSecurityPolicy('abc')).toBe(PROD_CSP('abc'));
  });

  it('uses a fresh nonce per request and forwards it to Next on the request', () => {
    vi.stubEnv('NODE_ENV', 'production');
    const a = proxy(new NextRequest('http://127.0.0.1:3710/login'));
    const b = proxy(new NextRequest('http://127.0.0.1:3710/login'));
    const na = nonceOf(a.headers.get('content-security-policy'));
    const nb = nonceOf(b.headers.get('content-security-policy'));
    expect(na).not.toBe(nb);
    expect(a.headers.get('x-middleware-request-x-nonce')).toBe(na);
    expect(a.headers.get('x-middleware-request-content-security-policy')).toBe(
      a.headers.get('content-security-policy'),
    );
    expect(b.headers.get('x-middleware-request-x-nonce')).toBe(nb);
    expect(b.headers.get('x-middleware-request-content-security-policy')).toBe(
      b.headers.get('content-security-policy'),
    );
  });

  it('never sets Strict-Transport-Security', () => {
    const page = proxy(new NextRequest('http://127.0.0.1:3710/login'));
    expect(page.headers.get('strict-transport-security')).toBeNull();
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    const api = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(api.headers.get('strict-transport-security')).toBeNull();
  });

  it('sets no nonce or CSP on proxied API requests', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(res.headers.get('content-security-policy')).toBeNull();
    expect(res.headers.get('x-middleware-request-x-nonce')).toBeNull();
  });
});

describe('SHIFDE_WEB_API_PROXY', () => {
  afterEach(() => vi.unstubAllEnvs());

  it.each(['/api/health', '/api/v1/x', '/api/metrics'])(
    'off answers 404 {"detail":"not found"} for %s without forwarding',
    async (p) => {
      vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
      vi.stubEnv('SHIFDE_WEB_API_PROXY', 'off');
      const res = proxy(new NextRequest(`http://127.0.0.1:3710${p}`));
      expect(res.status).toBe(404);
      expect(res.headers.get('content-type')).toBe('application/json');
      expect(await res.text()).toBe('{"detail":"not found"}');
      expect(res.headers.get('x-middleware-rewrite')).toBeNull();
    },
  );

  it.each([
    ['on', 'on'],
    ['unset', undefined],
    ['empty', ''],
    ['ON', 'ON'],
    ['padded on', ' on '],
  ])('%s still proxies', (_label, value) => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    vi.stubEnv('SHIFDE_WEB_API_PROXY', value);
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(res.headers.get('x-middleware-rewrite')).toBe('http://10.0.0.5:9000/api/health');
  });

  it.each(['OFF', 'yes', 'true', '0'])('treats %s as off (fail closed)', (value) => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    vi.stubEnv('SHIFDE_WEB_API_PROXY', value);
    const res = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(res.status).toBe(404);
    expect(res.headers.get('x-middleware-rewrite')).toBeNull();
  });

  it('is read per request', () => {
    vi.stubEnv('SHIFDE_API_ORIGIN', 'http://10.0.0.5:9000');
    vi.stubEnv('SHIFDE_WEB_API_PROXY', 'on');
    const a = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(a.headers.get('x-middleware-rewrite')).toBe('http://10.0.0.5:9000/api/health');
    vi.stubEnv('SHIFDE_WEB_API_PROXY', 'off');
    const b = proxy(new NextRequest('http://127.0.0.1:3710/api/health'));
    expect(b.status).toBe(404);
    expect(b.headers.get('x-middleware-rewrite')).toBeNull();
  });

  it('off still serves pages with their headers', () => {
    vi.stubEnv('SHIFDE_WEB_API_PROXY', 'off');
    const res = proxy(new NextRequest('http://127.0.0.1:3710/login'));
    expect(res.headers.get('x-middleware-next')).toBe('1');
    expect(res.headers.get('content-security-policy')).toContain("'nonce-");
  });
});

describe('proxy matcher', () => {
  // Loaded here, after the other tests: the helper pulls in Next's server console patch, which
  // throws on console.error outside a Next runtime (the invalid-origin test above logs one).
  let doesMatch: (url: string) => boolean;
  beforeAll(async () => {
    const { unstable_doesMiddlewareMatch } = await import('next/experimental/testing/server');
    doesMatch = (url) => unstable_doesMiddlewareMatch({ config, url });
  });

  it.each([
    '/_next/static/chunks/x.js',
    '/_next/image?url=x',
    '/favicon.ico',
    '/logo.png',
    '/fonts/a.woff2',
    '/x.css',
    '/x.map',
  ])('skips %s', (url) => {
    expect(doesMatch(url)).toBe(false);
  });

  it.each(['/login', '/o/demo-a/command', '/api/health'])('matches %s', (url) => {
    expect(doesMatch(url)).toBe(true);
  });
});
