'use client';

import { useMemo } from 'react';
import type { PermissionDef } from '@/lib/api/security-types';
import { Badge } from './ui';

export function groupByResource(catalog: readonly PermissionDef[]): [string, PermissionDef[]][] {
  const map = new Map<string, PermissionDef[]>();
  for (const p of catalog) {
    const list = map.get(p.resource_type) ?? [];
    list.push(p);
    map.set(p.resource_type, list);
  }
  return [...map.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([type, perms]) => [type, perms.sort((a, b) => a.key.localeCompare(b.key))]);
}

interface PermissionChecklistProps {
  catalog: readonly PermissionDef[];
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  /** Only these permissions are offered (e.g. an API key can only carry what its creator holds). */
  offered?: ReadonlySet<string>;
  /**
   * Privileged permissions are disabled unless the user holds them (escalation guard preview;
   * the server enforces it regardless).
   */
  held?: ReadonlySet<string>;
  disabled?: boolean;
}

/** Permission checklist grouped by resource type; privileged permissions are flagged (FR-AUTHZ). */
export function PermissionChecklist({
  catalog,
  selected,
  onChange,
  offered,
  held,
  disabled,
}: PermissionChecklistProps) {
  const groups = useMemo(
    () => groupByResource(offered ? catalog.filter((p) => offered.has(p.key)) : catalog),
    [catalog, offered],
  );

  function toggle(key: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(key);
    else next.delete(key);
    onChange(next);
  }

  if (groups.length === 0) {
    return <p className="text-sm text-slate-600">No permissions are available to select.</p>;
  }

  return (
    <div className="space-y-3">
      {groups.map(([type, perms]) => (
        <fieldset key={type} className="rounded-md border border-slate-200 p-3">
          <legend className="px-1 text-sm font-semibold text-slate-900">{type}</legend>
          <ul className="space-y-1">
            {perms.map((p) => {
              const blocked = p.is_privileged && held !== undefined && !held.has(p.key);
              return (
                <li key={p.key}>
                  <label className="flex flex-wrap items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={selected.has(p.key)}
                      disabled={disabled || (blocked && !selected.has(p.key))}
                      onChange={(e) => toggle(p.key, e.target.checked)}
                    />
                    <span className="font-mono text-xs">{p.key}</span>
                    {p.is_privileged && <Badge tone="warn">Privileged</Badge>}
                    {blocked && (
                      <span className="text-xs text-slate-600">(you do not hold this)</span>
                    )}
                  </label>
                  {p.description && <p className="ml-6 text-xs text-slate-600">{p.description}</p>}
                </li>
              );
            })}
          </ul>
        </fieldset>
      ))}
    </div>
  );
}
