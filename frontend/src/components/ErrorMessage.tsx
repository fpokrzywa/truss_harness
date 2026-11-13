'use client';

import { useState } from 'react';
import { ApiError } from '@/lib/api/errors';

interface ErrorMessageProps {
  error: unknown;
  /** Heading override; defaults to the problem title. */
  title?: string;
}

/** Structured error display: code, message, field errors, request id with copy (NFR-USAB-002). */
export function ErrorMessage({ error, title }: ErrorMessageProps) {
  const [copied, setCopied] = useState(false);
  const api = error instanceof ApiError ? error : null;
  const heading = title ?? api?.title ?? 'Something went wrong';
  const message =
    api?.detail ?? (error instanceof Error ? error.message : 'An unexpected error occurred.');

  async function copy(id: string) {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard unavailable (insecure context); id stays visible to copy by hand
    }
  }

  return (
    <div
      role="alert"
      className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
    >
      <p className="font-semibold">{heading}</p>
      {message !== heading && <p className="mt-1">{message}</p>}
      {api && api.fieldErrors.length > 0 && (
        <ul className="mt-2 list-disc pl-5">
          {api.fieldErrors.map((f, i) => (
            <li key={`${f.loc.join('.')}-${i}`}>
              {f.loc.length > 0 && <span className="font-mono">{f.loc.join('.')}: </span>}
              {f.msg}
            </li>
          ))}
        </ul>
      )}
      {api && (
        <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-red-800">
          <span>
            Code: <code className="font-mono">{api.code}</code>
          </span>
          {api.requestId && (
            <span className="flex items-center gap-2">
              Request ID: <code className="font-mono">{api.requestId}</code>
              <button
                type="button"
                className="rounded border border-red-300 bg-white px-1.5 py-0.5 hover:bg-red-100"
                onClick={() => void copy(api.requestId as string)}
              >
                {copied ? 'Copied' : 'Copy'} <span className="sr-only">request ID</span>
              </button>
            </span>
          )}
        </p>
      )}
    </div>
  );
}
