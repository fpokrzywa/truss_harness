import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { VIA_HEADER, canonicalPath, isMetricsPath, proxy } from './proxy';

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
