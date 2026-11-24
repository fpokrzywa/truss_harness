import { describe, expect, it } from 'vitest';
import {
  appRequestHref,
  assistHref,
  assistRequestHref,
  assistSettingsHref,
  generateHref,
  parseScope,
} from './scope';

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

describe('Ask SETH hrefs (SP7-7)', () => {
  it('builds the project screen href, with a context when given', () => {
    expect(assistHref('acme', 'eng', 'api')).toBe('/o/acme/w/eng/p/api/assist');
    expect(assistHref('acme', 'eng', 'api', null)).toBe('/o/acme/w/eng/p/api/assist');
    expect(assistHref('acme', 'eng', 'api', { kind: 'evaluation_run', id: 'e&1' })).toBe(
      '/o/acme/w/eng/p/api/assist?context=evaluation_run:e%261',
    );
  });

  it('builds one request href and the settings href', () => {
    expect(assistRequestHref('acme', 'x/y')).toBe('/o/acme/assist/x%2Fy');
    expect(assistSettingsHref('acme')).toBe('/o/acme/assist/settings');
    expect(assistSettingsHref('acme', 'eng', 'api')).toBe(
      '/o/acme/assist/settings?ws=eng&proj=api',
    );
    expect(assistSettingsHref('acme', 'eng')).toBe('/o/acme/assist/settings');
  });

  it('keeps the organization in scope on the organization-level screens', () => {
    expect(parseScope('/o/acme/assist/x')).toEqual({ org: 'acme', area: 'other' });
    expect(parseScope('/o/acme/assist/settings')).toEqual({ org: 'acme', area: 'other' });
  });
});
