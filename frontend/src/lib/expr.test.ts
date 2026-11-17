import { describe, expect, it } from 'vitest';
import { checkExpression } from './expr';

const ok = (s: string) => expect(checkExpression(s).ok).toBe(true);
const bad = (s: string, msg: RegExp) => {
  const r = checkExpression(s);
  expect(r.ok).toBe(false);
  expect(r.error).toMatch(msg);
};

describe('safe expression syntax check (mirrors backend safeexpr)', () => {
  it('accepts literals, comparisons, boolean ops, parentheses and dotted fields', () => {
    ok('true');
    ok('classify.output.score >= 0.8');
    ok("review.output.label == 'ok' and not review.output.flagged");
    ok('(a.b < 3 or c.d != "x") and e.f');
    ok('not (a == null)');
  });

  it('accepts what the server accepts: indexing, symbol operators, in / not in, signed numbers', () => {
    ok('inputs.rollup[0].max_open > params.threshold');
    ok("row['order id'] == 1");
    ok('a && b || !c');
    ok("'x' in a.list");
    ok('a.b not in c.d');
    ok('a > -1.5e3');
  });

  it('collects the field paths it reads', () => {
    expect(checkExpression('inputs.rollup[0].max_open > params.threshold').paths).toEqual([
      ['inputs', 'rollup', 0, 'max_open'],
      ['params', 'threshold'],
    ]);
  });

  it('rejects function calls', () => {
    bad('len(a.b) > 1', /Function calls are not allowed/);
    bad('a.b.lower() == "x"', /Function calls are not allowed/);
    bad('true()', /Unexpected/);
  });

  it('rejects what the server rejects', () => {
    bad('a[-1] == 1', /index/);
    bad('a[b] == 1', /index/);
    bad('a[0 == 1', /closing bracket/);
    bad('a.__class__ == 1', /"_"/);
    bad('_a == 1', /"_"/);
    bad('a.in == 1', /field name/);
    bad(`a == '${'x'.repeat(1000)}'`, /longer than/);
  });

  it('reports malformed input with a position', () => {
    bad('', /empty/);
    bad('a ==', /ended unexpectedly/);
    bad('(a == 1', /closing parenthesis/);
    bad('a == b == c', /Unexpected "=="/);
    bad("a == 'x", /Unterminated/);
    bad('a.', /ended unexpectedly|field name/);
    bad('a b', /Unexpected/);
    expect(checkExpression('a == $').pos).toBe(5);
  });
});
