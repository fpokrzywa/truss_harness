'use client';

import type { NodeTypeDef } from '@/lib/api/pipeline-types';
import { Badge } from '../admin/ui';
import { QueryError } from '../QueryError';
import { DRAG_MIME } from './PipelineCanvas';

/**
 * Palette from the node-type registry. Only `enabled` types can be added or dragged; the others are
 * listed greyed out with the server's reason (no fake nodes).
 */
export function NodePalette({
  types,
  loading,
  error,
  readOnly,
  onAdd,
}: {
  types: readonly NodeTypeDef[] | undefined;
  loading: boolean;
  error: unknown;
  readOnly: boolean;
  onAdd: (type: string) => void;
}) {
  if (loading) return <p role="status">Loading node types...</p>;
  if (error) return <QueryError error={error} />;
  const enabled = (types ?? []).filter((t) => t.enabled);
  const disabled = (types ?? []).filter((t) => !t.enabled);
  return (
    <div className="space-y-2">
      <ul aria-label="Node palette" className="flex flex-wrap gap-2">
        {enabled.map((t) => (
          <li key={t.type} className="flex items-center gap-1">
            {readOnly ? (
              <span className="text-sm">{t.label}</span>
            ) : (
              <button
                type="button"
                className="btn"
                draggable
                title={`${t.category} node. Click to add, or drag onto the canvas.`}
                aria-label={`Add ${t.label} node`}
                onClick={() => onAdd(t.type)}
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME, t.type);
                  e.dataTransfer.effectAllowed = 'copy';
                }}
              >
                {t.label}
              </button>
            )}
            {t.maturity !== 'validated' && <Badge tone="warn">{t.maturity}</Badge>}
          </li>
        ))}
      </ul>
      {enabled.length === 0 && (
        <p className="text-sm text-slate-600">No node types are enabled in this installation.</p>
      )}
      {disabled.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold text-slate-600">Not available</h4>
          <ul aria-label="Unavailable node types" className="mt-1 space-y-1">
            {disabled.map((t) => (
              <li key={t.type} className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
                <span>{t.label}</span>
                <Badge tone="neutral">{t.maturity}</Badge>
                <span className="text-xs">
                  {t.reason_disabled ?? 'Its executor is not enabled in this installation.'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
