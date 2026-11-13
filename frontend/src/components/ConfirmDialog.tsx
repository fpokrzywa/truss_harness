'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Modal confirmation built on native <dialog>: focus trap, Escape to cancel (NFR-USAB-001/003). */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (open && !dlg.open) dlg.showModal();
    if (!open && dlg.open) dlg.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault(); // let React state own visibility
        if (!busy) onCancel();
      }}
      className="m-auto w-full max-w-md rounded-lg border border-slate-300 p-0 shadow-xl backdrop:bg-slate-900/40"
    >
      {open && (
        <div className="p-5">
          <h2 id={titleId} className="text-lg font-semibold text-slate-900">
            {title}
          </h2>
          <div className="mt-2 text-sm text-slate-700">{children}</div>
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className="btn" onClick={onCancel} disabled={busy} autoFocus>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" onClick={onConfirm} disabled={busy}>
              {busy ? 'Working...' : confirmLabel}
            </button>
          </div>
        </div>
      )}
    </dialog>
  );
}
