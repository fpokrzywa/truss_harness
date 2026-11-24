import { describe, expect, it } from 'vitest';
import { appRequestHref, generateHref } from './scope';

describe('solution generator hrefs (SP5-7)', () => {
  it('builds the generate screen href, with the project when both slugs are given', () => {
    expect(generateHref('acme')).toBe('/o/acme/solutions/generate');
    expect(generateHref('acme', 'eng', 'api')).toBe('/o/acme/solutions/generate?ws=eng&proj=api');
    expect(generateHref('acme', 'eng')).toBe('/o/acme/solutions/generate');
  });

  it('builds one request href and encodes its parts', () => {
    expect(appRequestHref('acme', 'ar-1')).toBe('/o/acme/solutions/generate/ar-1');
    expect(appRequestHref('acme', 'ar-1', 'eng', 'api')).toBe(
      '/o/acme/solutions/generate/ar-1?ws=eng&proj=api',
    );
    expect(appRequestHref('a b', 'x/y', 'e&g', 'p')).toBe(
      '/o/a%20b/solutions/generate/x%2Fy?ws=e%26g&proj=p',
    );
  });
});
