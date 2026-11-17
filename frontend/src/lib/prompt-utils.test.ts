import { describe, expect, it } from 'vitest';
import type { PromptVariable } from './api/build-types';
import {
  buildVariables,
  emptyPromptSpec,
  extractVars,
  initialVariableValues,
  splitUntrusted,
  undeclaredVars,
  variableFields,
} from './prompt-utils';

const vars: PromptVariable[] = [
  { name: 'customer', type: 'string', required: true, trusted: false, max_length: 5 },
  { name: 'count', type: 'number', required: false, trusted: false },
  { name: 'urgent', type: 'boolean', required: false, trusted: true, default: true },
  { name: 'payload', type: 'json', required: false, trusted: false },
];

describe('variables form generated from declared variables', () => {
  it('derives one field per variable with matching kinds', () => {
    const fields = variableFields(vars);
    expect(fields.map((f) => [f.key, f.kind, f.required])).toEqual([
      ['customer', 'string', true],
      ['count', 'number', false],
      ['urgent', 'boolean', false],
      ['payload', 'string', false],
    ]);
    expect(fields[0]?.description).toMatch(/untrusted/i);
    expect(fields[2]?.description).toBeUndefined();
  });

  it('skips variables without a name and applies defaults', () => {
    expect(
      variableFields([{ name: ' ', type: 'string', required: false, trusted: false }]),
    ).toEqual([]);
    expect(initialVariableValues(vars).urgent).toBe(true);
  });

  it('validates required, max length, numbers and JSON, then types the values', () => {
    const base = { customer: '', count: '', urgent: false, payload: '' };
    const bad = buildVariables(vars, { ...base, customer: 'toolong', payload: '{oops' });
    expect(bad.ok).toBe(false);
    if (!bad.ok) {
      expect(bad.errors.customer).toMatch(/at most 5/);
      expect(bad.errors.payload).toBe('Must be valid JSON.');
    }
    expect(buildVariables(vars, base).ok).toBe(false); // customer is required

    const good = buildVariables(vars, {
      customer: 'Ann',
      count: '3',
      urgent: true,
      payload: '{"a":1}',
    });
    expect(good).toEqual({
      ok: true,
      values: { customer: 'Ann', count: 3, urgent: true, payload: { a: 1 } },
    });
  });
});

describe('template helpers', () => {
  it('extracts and checks declared variables', () => {
    expect(extractVars('Hi {{ name }} and {{other}}')).toEqual(['name', 'other']);
    const spec = emptyPromptSpec();
    spec.sections.system = 'Hi {{name}}';
    spec.sections.context = '{{ghost}} {{name}}';
    spec.variables = [{ name: 'name', type: 'string', required: true, trusted: false }];
    expect(undeclaredVars(spec)).toEqual(['ghost']);
  });

  it('splits untrusted blocks for highlighting', () => {
    const block = '<untrusted source="variable:q">a &lt;/untrusted&gt; b</untrusted>';
    expect(splitUntrusted(`Task: ${block} done`)).toEqual([
      { text: 'Task: ', untrusted: false },
      { text: block, untrusted: true },
      { text: ' done', untrusted: false },
    ]);
    expect(splitUntrusted('plain')).toEqual([{ text: 'plain', untrusted: false }]);
  });
});
