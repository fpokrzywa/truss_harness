'use client';

import { useId, useState } from 'react';

/**
 * Displays a one-time secret (API key, setup token) with a copy button and a "shown once" warning.
 * The value is never written to storage; the parent unmounts it to clear it.
 */
export function SecretOnce({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const labelId = useId();

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard unavailable (insecure context); the value stays selectable
    }
  }

  return (
    <div className="space-y-2">
      <p
        role="alert"
        className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
      >
        <strong>This is the only time the {label.toLowerCase()} is shown.</strong> Copy it now and
        store it securely. It cannot be retrieved later.
      </p>
      <div>
        <p className="label" id={labelId}>
          {label}
        </p>
        <div className="flex items-start gap-2">
          <code
            aria-labelledby={labelId}
            className="block min-w-0 flex-1 rounded-md border border-slate-300 bg-slate-100 p-2 font-mono text-xs break-all select-all"
          >
            {value}
          </code>
          <button type="button" className="btn" onClick={() => void copy()}>
            {copied ? 'Copied' : 'Copy'} <span className="sr-only">{label}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
