'use client';

import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

/** Fades and slides its content in the first time it scrolls into view. `index` staggers siblings. */
export function Reveal({
  index = 0,
  className = '',
  children,
}: {
  index?: number;
  className?: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [revealed, setRevealed] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setRevealed(true);
          observer.disconnect();
        }
      },
      { threshold: 0.05 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      data-revealed={revealed}
      className={`cc-reveal ${className}`}
      style={{ '--cc-i': index } as CSSProperties}
    >
      {children}
    </div>
  );
}
