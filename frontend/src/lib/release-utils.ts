import { ApiError } from './api/errors';
import type { AssertionKind, EvaluationCase } from './api/evaluation-types';
import type { Diagnostic } from './api/packaging-types';

/** The first 12 characters of a digest (the full value belongs in `title`). */
export const shortDigest = (digest: string) => digest.slice(0, 12);

/**
 * The compatibility report of a refused seal (`422 release_incompatible`), rebuilt from the
 * problem's field errors (`loc: ["compatibility", ...path.split(".")]`, `msg`, `type`); null for
 * any other error.
 */
export function compatibilityReport(error: unknown): Diagnostic[] | null {
  if (!(error instanceof ApiError) || error.code !== 'release_incompatible') return null;
  return error.fieldErrors
    .filter((f) => f.loc[0] === 'compatibility')
    .map((f) => {
      const supported = f.type === 'supported';
      return {
        path: f.loc.slice(1).join('.'),
        status: supported ? 'supported' : 'unsupported',
        reason: supported ? null : f.msg,
      };
    });
}

export const MAX_CASES = 200;
export const MAX_ASSERTIONS = 50;
export const MAX_PATTERN = 200;
export const MAX_CASES_BYTES = 1_048_576;
export const ASSERTION_KINDS: readonly AssertionKind[] = [
  'equals',
  'contains',
  'not_contains',
  'regex',
  'json_equals',
];

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v !== '';

/** Parses and checks dataset cases against the server's limits before they are posted. */
export function validateCases(text: string): { cases: EvaluationCase[] } | { error: string } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { error: 'The cases are not valid JSON.' };
  }
  if (!Array.isArray(value)) return { error: 'The cases must be a JSON array.' };
  if (value.length === 0) return { error: 'Add at least one case.' };
  if (value.length > MAX_CASES) return { error: `A dataset holds at most ${MAX_CASES} cases.` };

  const caseIds = new Set<string>();
  for (const [i, c] of value.entries()) {
    const at = `Case ${i + 1}`;
    if (!isObject(c) || !nonEmpty(c.case_id)) return { error: `${at} needs a case_id.` };
    if (!isObject(c.input)) return { error: `${at} (${c.case_id}) needs an input object.` };
    if (caseIds.has(c.case_id)) return { error: `Duplicate case_id "${c.case_id}".` };
    caseIds.add(c.case_id);
    const assertions = c.assertions;
    if (!Array.isArray(assertions) || assertions.length === 0)
      return { error: `Case "${c.case_id}" needs at least one assertion.` };
    if (assertions.length > MAX_ASSERTIONS)
      return { error: `Case "${c.case_id}" has more than ${MAX_ASSERTIONS} assertions.` };
    const assertionIds = new Set<string>();
    for (const a of assertions as unknown[]) {
      if (!isObject(a) || !nonEmpty(a.id))
        return { error: `Case "${c.case_id}" has an assertion without an id.` };
      if (assertionIds.has(a.id))
        return { error: `Case "${c.case_id}" has a duplicate assertion id "${a.id}".` };
      assertionIds.add(a.id);
      if (!ASSERTION_KINDS.includes(a.kind as AssertionKind))
        return {
          error: `Assertion "${a.id}" in case "${c.case_id}" has an unknown kind; use one of ${ASSERTION_KINDS.join(', ')}.`,
        };
      if (a.kind === 'regex' && (typeof a.value !== 'string' || a.value.length > MAX_PATTERN))
        return {
          error: `Assertion "${a.id}" in case "${c.case_id}": a regex is at most ${MAX_PATTERN} characters.`,
        };
    }
  }
  if (new TextEncoder().encode(JSON.stringify(value)).length > MAX_CASES_BYTES)
    return { error: `The cases exceed ${MAX_CASES_BYTES} bytes.` };
  return { cases: value as EvaluationCase[] };
}
