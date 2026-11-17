'use client';

import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

interface PipelineRunState {
  /** The run whose per-node statuses overlay the canvas (a draft test run or a published execution). */
  runId: string | undefined;
  setRunId: (id: string | undefined) => void;
}

const Ctx = createContext<PipelineRunState>({ runId: undefined, setRunId: () => undefined });

/** Shares the active run between the Test/Run panels and the canvas so statuses show on the graph. */
export function PipelineRunProvider({ children }: { children: ReactNode }) {
  const [runId, setRunId] = useState<string | undefined>(undefined);
  const value = useMemo(() => ({ runId, setRunId }), [runId]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const usePipelineRun = () => useContext(Ctx);
