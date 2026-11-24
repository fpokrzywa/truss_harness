import { describe, expect, it } from 'vitest';
import { CONTEXT_KINDS } from './api/assist-types';
import { contextHref, parseContext, reasonText, specialistTitle } from './assist';

const ID = '01900000-0000-7000-8000-0000000000a1';

describe('parseContext', () => {
  it.each(CONTEXT_KINDS)('accepts %s with a UUID', (kind) => {
    expect(parseContext(`${kind}:${ID}`)).toEqual({ kind, id: ID });
  });

  it.each([
    null,
    '',
    'release',
    'release:',
    `:${ID}`,
    ID,
    `agent:${ID}`,
    `Release:${ID}`,
    'release:r-1',
    `release:${ID}x`,
    `release:${ID}:x`,
    `toString:${ID}`,
  ])('ignores %s', (value) => {
    expect(parseContext(value)).toBeNull();
  });
});

describe('contextHref', () => {
  const project = { ws: 'eng', proj: 'api' };

  it('links each kind that has a page', () => {
    const href = (kind: (typeof CONTEXT_KINDS)[number]) =>
      contextHref('acme', { kind, id: ID }, project);
    expect(href('solution')).toBe(`/o/acme/solutions/${ID}?ws=eng&proj=api`);
    expect(href('release')).toBe(`/o/acme/releases/${ID}`);
    expect(href('plan')).toBe(`/o/acme/deliver/plans/${ID}`);
    expect(href('deployment')).toBe(`/o/acme/deliver/deployments/${ID}`);
    expect(href('evaluation_run')).toBe(`/o/acme/evaluations/${ID}`);
    expect(href('target')).toBeNull();
  });

  it('has no solution link without its project', () => {
    expect(contextHref('acme', { kind: 'solution', id: ID }, null)).toBeNull();
  });
});

describe('reasonText', () => {
  it.each([
    'route_ambiguous',
    'route_unclear',
    'specialist_disabled',
    'specialists_not_installed',
    'specialist_not_permitted',
    'specialist_modified',
    'router_model_missing',
    'access_revoked',
    'model_call_unknown',
  ])('has one plain sentence for %s', (code) => {
    expect(reasonText(code)).toMatch(/^[A-Z][^.]*\.$/);
  });

  it('shows an unknown code as it is', () => {
    expect(reasonText('budget_denied')).toBe('budget_denied');
    expect(reasonText('constructor')).toBe('constructor');
  });
});

it('specialistTitle falls back to the key', () => {
  expect(specialistTitle(undefined, 'cost')).toBe('cost');
});
