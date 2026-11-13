import { describe, expect, it } from 'vitest';
import { apiOrigin, DEFAULT_API_ORIGIN } from './api-origin';

describe('apiOrigin', () => {
  it('defaults to the loopback API', () => {
    expect(apiOrigin({})).toBe(DEFAULT_API_ORIGIN);
    expect(apiOrigin({ SHIFDE_API_ORIGIN: '  ' })).toBe(DEFAULT_API_ORIGIN);
  });
  it('reduces the value to its origin', () => {
    expect(apiOrigin({ SHIFDE_API_ORIGIN: 'https://api.example.test:8443/ignored/path' })).toBe(
      'https://api.example.test:8443',
    );
  });
  it('rejects invalid and non-http values', () => {
    expect(() => apiOrigin({ SHIFDE_API_ORIGIN: 'nope' })).toThrow(/not a valid URL/);
    expect(() => apiOrigin({ SHIFDE_API_ORIGIN: 'file:///etc/passwd' })).toThrow(/http\(s\)/);
  });
});
