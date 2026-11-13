import { describe, expect, it } from 'vitest';
import { safeNextPath, slugify, validateEmail, validateName, validateSlug } from './validation';

describe('validateSlug', () => {
  it.each(['a', 'ab', 'a1', 'my-org', 'a-b-c', '0abc', 'x'.repeat(63)])('accepts %s', (s) => {
    expect(validateSlug(s)).toBeNull();
  });
  it.each(['', '-a', 'a-', 'A', 'a_b', 'a b', 'x'.repeat(64), 'é', '--'])('rejects %j', (s) => {
    expect(validateSlug(s)).not.toBeNull();
  });
});

describe('validateName / validateEmail', () => {
  it('requires a non-blank name up to 200 chars', () => {
    expect(validateName('   ')).not.toBeNull();
    expect(validateName('Acme')).toBeNull();
    expect(validateName('x'.repeat(201))).not.toBeNull();
  });
  it('checks email shape', () => {
    expect(validateEmail('')).not.toBeNull();
    expect(validateEmail('nope')).not.toBeNull();
    expect(validateEmail('a@b.co')).toBeNull();
  });
});

describe('slugify', () => {
  it('produces slugs that satisfy the slug rule', () => {
    for (const name of ['Acme Corp', '  Hello, World!  ', 'Ünïcode Name', 'x'.repeat(100) + '-']) {
      const s = slugify(name);
      expect(s === '' || validateSlug(s) === null).toBe(true);
    }
    expect(slugify('Acme Corp')).toBe('acme-corp');
  });
});

describe('safeNextPath', () => {
  it.each([
    '/\t/evil.com',
    '/\n/evil.com',
    '/\r\n/evil.com',
    '/%09/evil.com',
    '/%0a/evil.com',
    '/%0d%0a/evil.com',
    '//evil.com',
    '/\\evil.com',
    '/%5cevil.com',
    '/%2f/evil.com',
    'https://evil.com',
    'javascript:alert(1)',
    '/%zz',
    '',
  ])('rejects %j', (next) => {
    expect(safeNextPath(next)).toBe('/');
  });

  it('rejects null and undefined', () => {
    expect(safeNextPath(null)).toBe('/');
    expect(safeNextPath(undefined)).toBe('/');
  });

  it('keeps valid paths with query and hash', () => {
    expect(safeNextPath('/o/acme')).toBe('/o/acme');
    expect(safeNextPath('/o/acme/w/eng?tab=1&x=a%20b#section')).toBe(
      '/o/acme/w/eng?tab=1&x=a%20b#section',
    );
  });
});
