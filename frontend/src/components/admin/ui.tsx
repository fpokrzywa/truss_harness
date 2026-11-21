'use client';

import type { UseQueryResult } from '@tanstack/react-query';
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { ErrorMessage } from '../ErrorMessage';
import { QueryError } from '../QueryError';

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
        {description && <p className="mt-1 max-w-2xl text-sm text-slate-600">{description}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
  );
}

/** Loading and error states shared by every admin list (403/404 render the neutral no-access view). */
export function QueryGate<T>({
  query,
  noun,
  children,
}: {
  query: UseQueryResult<T>;
  noun: string;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) {
    return (
      <p role="status" className="text-sm text-slate-600">
        Loading {noun}...
      </p>
    );
  }
  if (query.isError) return <QueryError error={query.error} />;
  return <>{children(query.data)}</>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="p-4 text-sm text-slate-600">{children}</p>;
}

export function Badge({
  tone = 'neutral',
  children,
}: {
  tone?: 'neutral' | 'good' | 'warn' | 'bad';
  children: ReactNode;
}) {
  const tones = {
    neutral: 'border-slate-300 bg-slate-100 text-slate-700',
    good: 'border-green-300 bg-green-50 text-green-800',
    warn: 'border-amber-300 bg-amber-50 text-amber-900',
    bad: 'border-red-300 bg-red-50 text-red-800',
  };
  return <span className={`badge ${tones[tone]}`}>{children}</span>;
}

interface FieldProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: 'text' | 'email' | 'password' | 'number' | 'datetime-local' | 'date';
  error?: string | null;
  help?: string;
  required?: boolean;
  multiline?: boolean;
  placeholder?: string;
  autoComplete?: string;
  min?: number;
  max?: number;
  maxLength?: number;
  monospace?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
}

/** Labelled input with optional help and error text wired through aria-describedby. */
export function TextField({
  label,
  value,
  onChange,
  type = 'text',
  error,
  help,
  required,
  multiline,
  placeholder,
  autoComplete,
  min,
  max,
  maxLength,
  monospace,
  disabled,
  readOnly,
}: FieldProps) {
  const id = useId();
  const describedBy = [help ? `${id}-help` : null, error ? `${id}-err` : null]
    .filter(Boolean)
    .join(' ');
  const common = {
    id,
    className: `input${monospace ? ' font-mono' : ''}`,
    value,
    required,
    disabled,
    readOnly,
    placeholder,
    maxLength,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': describedBy || undefined,
  } as const;
  return (
    <div>
      <label htmlFor={id} className="label">
        {label}
      </label>
      {multiline ? (
        <textarea {...common} rows={2} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input
          {...common}
          type={type}
          min={min}
          max={max}
          autoComplete={autoComplete}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {help && (
        <p id={`${id}-help`} className="mt-1 text-xs text-slate-600">
          {help}
        </p>
      )}
      {error && (
        <p id={`${id}-err`} className="mt-1 text-sm text-red-700">
          {error}
        </p>
      )}
    </div>
  );
}

export function SelectField({
  label,
  value,
  onChange,
  options,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="label">
        {label}
      </label>
      <select
        id={id}
        className="input"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/**
 * Modal built on native <dialog> (focus trap, Escape to close). Mount it only while open: unmounting
 * discards all local state, which is how one-time secrets are cleared on close.
 */
export function Modal({
  title,
  onClose,
  busy = false,
  wide = false,
  children,
}: {
  title: string;
  onClose: () => void;
  busy?: boolean;
  wide?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dlg = ref.current;
    if (dlg && !dlg.open) dlg.showModal();
    return () => {
      if (dlg?.open) dlg.close();
    };
  }, []);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault(); // React owns visibility
        if (!busy) onClose();
      }}
      className={`m-auto w-full ${wide ? 'max-w-2xl' : 'max-w-md'} rounded-lg border border-slate-300 p-0 shadow-xl backdrop:bg-slate-900/40`}
    >
      <div className="max-h-[85vh] overflow-y-auto p-5">
        <h2 id={titleId} className="text-lg font-semibold text-slate-900">
          {title}
        </h2>
        <div className="mt-3">{children}</div>
      </div>
    </dialog>
  );
}

/** Shared footer for modal forms. */
export function ModalActions({
  onCancel,
  submitLabel,
  busy,
  submitDisabled,
  cancelLabel = 'Cancel',
}: {
  onCancel: () => void;
  submitLabel: string;
  busy: boolean;
  submitDisabled?: boolean;
  cancelLabel?: string;
}) {
  return (
    <div className="mt-5 flex justify-end gap-2">
      <button type="button" className="btn" onClick={onCancel} disabled={busy}>
        {cancelLabel}
      </button>
      <button type="submit" className="btn btn-primary" disabled={busy || submitDisabled}>
        {busy ? 'Working...' : submitLabel}
      </button>
    </div>
  );
}

export type ActionResult<T> = { ok: true; value: T } | { ok: false };

/**
 * Runs an async mutation with busy/error state. Results are returned to the caller and not cached,
 * so one-time secrets in responses live only where the caller puts them.
 */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<ActionResult<T>> => {
    setBusy(true);
    setError(null);
    try {
      return { ok: true, value: await fn() };
    } catch (e) {
      setError(e);
      return { ok: false };
    } finally {
      setBusy(false);
    }
  }, []);
  const reset = useCallback(() => setError(null), []);
  return { busy, error, run, reset };
}

export function ActionError({ error, title }: { error: unknown; title: string }) {
  if (error === null || error === undefined) return null;
  return <ErrorMessage error={error} title={title} />;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '-';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}
