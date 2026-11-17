'use client';

import type { ValidationIssue, ValidationResult } from '@/lib/api/build-types';

function groupByCode(issues: readonly ValidationIssue[]): [string, ValidationIssue[]][] {
  const map = new Map<string, ValidationIssue[]>();
  for (const i of issues) {
    const code = i.code ?? 'invalid';
    map.set(code, [...(map.get(code) ?? []), i]);
  }
  return [...map.entries()];
}

const where = (i: ValidationIssue): string =>
  i.node_id
    ? `node ${i.node_id}`
    : i.edge
      ? `edge ${i.edge.from} -> ${i.edge.to}`
      : i.path || '(pipeline)';

/** Server validation (rules 1-8): errors and warnings grouped by code, plus the execution order. */
export function PipelineValidationPanel({ result }: { result: ValidationResult }) {
  const warnings = result.warnings ?? [];
  return (
    <section aria-label="Validation results" className="card space-y-3">
      <h3 className="text-sm font-semibold">
        {result.valid
          ? 'Pipeline is valid'
          : `Pipeline has ${result.errors.length} error${result.errors.length === 1 ? '' : 's'}`}
      </h3>
      {groupByCode(result.errors).map(([code, items]) => (
        <div key={code}>
          <h4 className="font-mono text-xs font-semibold text-red-800">{code}</h4>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-red-800">
            {items.map((e, i) => (
              <li key={`${where(e)}-${i}`}>
                <span className="font-medium">{where(e)}:</span> {e.message}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {warnings.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-amber-900">Warnings</h4>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-amber-900">
            {warnings.map((w, i) => (
              <li key={`${where(w)}-${i}`}>
                <span className="font-medium">{where(w)}:</span> {w.message}
                {w.code && <span className="ml-1 font-mono text-xs">({w.code})</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {result.topo_order && result.topo_order.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-slate-700">Execution order</h4>
          <ol aria-label="Topological order" className="mt-1 flex flex-wrap gap-1 text-sm">
            {result.topo_order.map((id, i) => (
              <li key={id} className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs">
                {i + 1}. {id}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
