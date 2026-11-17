'use client';

import { useEffect, useState } from 'react';
import type { ReadoutLine } from '@/lib/command';
import { ToneGlyph } from './ToneGlyph';

const PRINT_MS = 180;

/** Prints the current state line by line when the page opens. Lines added later appear in turn. */
export function StartupReadout({ lines }: { lines: readonly ReadoutLine[] }) {
  const [printed, setPrinted] = useState(0);
  const done = printed >= lines.length;

  useEffect(() => {
    if (done) return;
    const timer = setInterval(() => setPrinted((p) => p + 1), PRINT_MS);
    return () => clearInterval(timer);
  }, [done]);

  return (
    <ol className="space-y-2 border border-shi-blue-slate bg-shi-grey p-4 font-roboto-mono text-sm leading-relaxed">
      {lines.map((line, i) => (
        <li
          key={line.key}
          data-printed={i < printed}
          className="cc-line grid grid-cols-[1.25rem_1fr] gap-x-3 md:grid-cols-[1.25rem_11rem_1fr]"
        >
          <span className="pt-1">
            <ToneGlyph tone={line.tone} size={13} />
          </span>
          <span className="font-medium text-shi-ink">{line.key}</span>
          <span
            className={`col-start-2 md:col-start-3 ${line.tone === 'good' ? 'text-shi-green' : 'text-shi-ink'}`}
          >
            {line.value}
            {i === printed - 1 && <span aria-hidden="true" className="cc-caret" />}
          </span>
        </li>
      ))}
    </ol>
  );
}
