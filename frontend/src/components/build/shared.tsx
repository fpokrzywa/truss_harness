'use client';

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { Artifact, ArtifactKind, ArtifactScope, VersionState } from '@/lib/api/build-types';
import { useScopeOptions } from '@/lib/security-hooks';
import { buildHref } from '@/lib/scope';
import { Badge, TextField } from '../admin/ui';
import {
  SectionHostContext,
  SectionShownContext,
  sectionPanelId,
  sectionTabId,
  useSectionHost,
} from './SectionTabs';

export const KIND_INFO: Record<
  ArtifactKind,
  { label: string; singular: string; perm: string; blurb: string }
> = {
  prompts: {
    label: 'Prompts',
    singular: 'prompt',
    perm: 'prompt',
    blurb: 'Versioned prompt templates with typed variables, composition and evaluation criteria.',
  },
  agents: {
    label: 'Agents',
    singular: 'agent',
    perm: 'agent',
    blurb: 'Versioned agent definitions assembling a prompt, models, tools and skills.',
  },
  tools: {
    label: 'Tools',
    singular: 'tool',
    perm: 'tool',
    blurb: 'Governed tool definitions with executor bindings, schemas and side-effect classes.',
  },
  orchestrations: {
    label: 'Orchestrations',
    singular: 'orchestration',
    perm: 'orchestration',
    blurb:
      'Versioned graphs of agent, tool, condition, approval and parallel nodes executed by the graph executor.',
  },
  pipelines: {
    label: 'Pipelines',
    singular: 'pipeline',
    perm: 'pipeline',
    blurb:
      'Versioned data and AI pipelines: typed nodes (sources, transforms, models, agents, approvals, sinks) wired as a DAG.',
  },
  ontologies: {
    label: 'Ontologies',
    singular: 'ontology',
    perm: 'ontology',
    blurb:
      'Versioned logical models of object types and relationships, mapped to live source connections. Data is never copied.',
  },
  skills: {
    label: 'Skills',
    singular: 'skill',
    perm: 'skill',
    blurb: 'Reusable instructional skills. Skills never execute anything.',
  },
};

export const artifactHref = (org: string, kind: ArtifactKind, id: string) =>
  `${buildHref(org, kind)}/${encodeURIComponent(id)}`;

export function VersionStateBadge({ state }: { state: VersionState }) {
  const tone =
    state === 'published'
      ? 'good'
      : state === 'draft'
        ? 'warn'
        : state === 'retired'
          ? 'bad'
          : 'neutral';
  return <Badge tone={tone}>{state}</Badge>;
}

export function ScopeLabel({ org, scope }: { org: string; scope: ArtifactScope }) {
  const { options } = useScopeOptions(org);
  if (scope.type === 'tenant') return <>Organization</>;
  const found = options.find((o) => o.id === scope.id);
  return (
    <>
      {scope.type === 'workspace' ? 'Workspace' : 'Project'}: {found?.label ?? scope.id}
    </>
  );
}

export function latestLabel(a: Artifact): string {
  return a.latest_published_version ? `v${a.latest_published_version.version_no}` : 'Unpublished';
}

export function JsonView({ value, label }: { value: unknown; label: string }) {
  return (
    <pre
      aria-label={label}
      tabIndex={0}
      className="max-h-96 overflow-auto rounded bg-slate-100 p-2 font-mono text-xs"
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Section({
  title,
  help,
  plain = false,
  children,
}: {
  title: string;
  help?: string;
  /** No card: for a section that wraps content which brings its own cards. */
  plain?: boolean;
  children: ReactNode;
}) {
  const host = useSectionHost();
  const id = useId();
  const ref = useRef<HTMLDivElement>(null);
  const register = host?.register;

  useLayoutEffect(() => {
    if (!register || !ref.current) return;
    return register({ id, title, el: ref.current });
  }, [register, id, title]);

  // Latched: the first render happens before registration, when nothing is active yet.
  const active = !host || host.activeId === id;
  const [shown, setShown] = useState(active);
  if (active && !shown) setShown(true);

  const tabbed = !!host && host.count > 1;
  const fieldset = (
    <fieldset className={plain ? 'space-y-3' : 'card space-y-3'}>
      <legend className={tabbed ? 'sr-only' : 'px-1 text-sm font-semibold text-slate-900'}>
        {title}
      </legend>
      {help && <p className="text-xs text-slate-600">{help}</p>}
      {/* A section inside a section stays a plain card, not another tab. */}
      <SectionHostContext.Provider value={null}>
        <SectionShownContext.Provider value={shown}>{children}</SectionShownContext.Provider>
      </SectionHostContext.Provider>
    </fieldset>
  );
  if (!host) return fieldset;
  return (
    <div
      ref={ref}
      role={tabbed ? 'tabpanel' : undefined}
      id={sectionPanelId(id)}
      aria-labelledby={tabbed ? sectionTabId(id) : undefined}
      hidden={tabbed && host.activeId !== id}
    >
      {fieldset}
    </div>
  );
}

export function Check({
  label,
  checked,
  onChange,
  disabled,
  help,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  help?: string;
}) {
  return (
    <div>
      <label className="flex items-center gap-2 text-sm font-medium text-slate-800">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
        />
        {label}
      </label>
      {help && <p className="mt-1 text-xs text-slate-600">{help}</p>}
    </div>
  );
}

/** Parses a number input into a number (empty = undefined) so specs never carry NaN. */
export function num(text: string): number | undefined {
  if (text.trim() === '') return undefined;
  const n = Number(text);
  return Number.isFinite(n) ? n : undefined;
}

let areaSeq = 0;

/** Labelled multi-line text input with a configurable height. */
export function Area({
  label,
  value,
  onChange,
  rows = 4,
  readOnly,
  help,
  monospace,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  readOnly?: boolean;
  help?: string;
  monospace?: boolean;
}) {
  const [id] = useState(() => `area-${areaSeq++}`);
  return (
    <div>
      <label htmlFor={id} className="label">
        {label}
      </label>
      <textarea
        id={id}
        className={`input${monospace ? ' font-mono' : ''}`}
        rows={rows}
        value={value}
        readOnly={readOnly}
        aria-describedby={help ? `${id}-help` : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {help && (
        <p id={`${id}-help`} className="mt-1 text-xs text-slate-600">
          {help}
        </p>
      )}
    </div>
  );
}

/**
 * JSON textarea that keeps its own text so half-typed JSON is never lost. Valid JSON (and passing
 * `check`) is committed through onChange; otherwise the problems are shown and reported via onInvalid,
 * which the draft editor uses to block save and publish.
 */
export function JsonField({
  label,
  value,
  onChange,
  onInvalid,
  check,
  readOnly,
  rows = 6,
  help,
}: {
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  onInvalid?: (message: string | null) => void;
  /** Extra structural checks; return error messages. */
  check?: (parsed: unknown) => string[];
  readOnly?: boolean;
  rows?: number;
  help?: string;
}) {
  const [text, setText] = useState(() =>
    value === undefined ? '' : JSON.stringify(value, null, 2),
  );
  const [errors, setErrors] = useState<string[]>([]);
  // Keep the latest callback in a ref so inline arrow props do not retrigger the effects.
  const report = useRef(onInvalid);
  useEffect(() => {
    report.current = onInvalid;
  });
  useEffect(() => {
    report.current?.(errors.length ? `${label}: ${errors[0]}` : null);
  }, [errors, label]);
  useEffect(() => {
    const r = report;
    return () => r.current?.(null);
  }, []);

  function edit(next: string) {
    setText(next);
    if (next.trim() === '') {
      setErrors([]);
      onChange(undefined);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(next);
    } catch (e) {
      setErrors([`Not valid JSON: ${e instanceof Error ? e.message : 'parse error'}`]);
      return;
    }
    const problems = check?.(parsed) ?? [];
    setErrors(problems);
    if (problems.length === 0) onChange(parsed);
  }

  return (
    <div>
      <Area
        label={label}
        value={text}
        onChange={edit}
        rows={rows}
        readOnly={readOnly}
        help={help}
        monospace
      />
      {errors.length > 0 && (
        <ul role="alert" className="mt-1 list-disc pl-5 text-sm text-red-700">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Number input with a required numeric value in the spec. It keeps its own text so clearing the field
 * to retype does not snap to the fallback; the spec holds the fallback while the field is empty.
 */
export function NumberField({
  label,
  value,
  fallback,
  onChange,
  min,
  disabled,
  error,
}: {
  label: string;
  value: number;
  fallback: number;
  onChange: (value: number) => void;
  min?: number;
  disabled?: boolean;
  error?: string | null;
}) {
  const [text, setText] = useState(String(value));
  const parsed = num(text);
  // Resync only when the spec changed from outside (not while the field is empty or mid-typing).
  const shown = text.trim() !== '' && parsed !== value ? String(value) : text;
  return (
    <TextField
      label={label}
      type="number"
      min={min}
      disabled={disabled}
      error={error}
      value={shown}
      onChange={(v) => {
        setText(v);
        onChange(num(v) ?? fallback);
      }}
    />
  );
}
