'use client';

import { useEffect, useState } from 'react';

/** The current time in milliseconds, re-rendering once a second (for countdowns). */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}
