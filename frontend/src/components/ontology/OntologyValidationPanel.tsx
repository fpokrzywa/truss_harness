'use client';

import type { ValidationIssue, ValidationResult } from '@/lib/api/build-types';

const CODE_HELP: Record<string, string> = {
  duplicate_key: 'Duplicate key',
  unknown_namespace: 'Unknown namespace',
  invalid_type: 'Invalid property type',
  invalid_primary_key: 'Invalid primary key',
  dangling_relationship: 'Relationship endpoint missing',
  unmapped_object_type: 'Object type has no source mapping',
  unmapped_relationship: 'Relationship has no source mapping',
  mapping_invalid: 'Mapping invalid',
  type_incompatible: 'Column type incompatible with property type',
  capability_missing: 'Connection adapter lacks a required capability',
  action_tool_invalid: 'Action tool version invalid',
};

const targetOf = (i: ValidationIssue): string | null => i.node_id ?? i.key ?? null;

/** Validation results grouped by contract code; the target key matches the highlighted node or edge. */
export function OntologyValidationPanel({ result }: { result: ValidationResult }) {
  const groups = new Map<string, ValidationIssue[]>();
  for (const e of result.errors) {
    const code = e.code ?? 'other';
    groups.set(code, [...(groups.get(code) ?? []), e]);
  }
  return (
    <section aria-label="Validation results" className="card space-y-3">
      <h3 className="text-sm font-semibold">
        {result.valid ? 'Draft is valid' : `Draft has ${result.errors.length} error(s)`}
      </h3>
      {[...groups].map(([code, issues]) => (
        <div key={code}>
          <h4 className="text-sm font-medium text-red-900">
            <span className="font-mono">{code}</span>
            {CODE_HELP[code] ? `: ${CODE_HELP[code]}` : ''}
          </h4>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-red-800">
            {issues.map((e, n) => (
              <li key={`${e.path}-${n}`}>
                {targetOf(e) && (
                  <code className="mr-1 rounded bg-red-50 px-1 font-mono text-xs">
                    {targetOf(e)}
                  </code>
                )}
                {e.message}
                {e.path && <span className="ml-1 font-mono text-xs text-slate-500">{e.path}</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {(result.warnings?.length ?? 0) > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-sm text-amber-900">
          {result.warnings?.map((w, n) => (
            <li key={`${w.path}-${n}`}>
              {targetOf(w) && <code className="mr-1 font-mono text-xs">{targetOf(w)}</code>}
              {w.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
