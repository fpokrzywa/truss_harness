import type { Adapter, Maturity } from './api/integrate-types';

const RANK: Record<Maturity, number> = { planned: 0, experimental: 1, validated: 2 };

/** Overall maturity of an adapter: its best capability. An adapter with no capabilities is planned. */
export function adapterMaturity(adapter: Pick<Adapter, 'capabilities'> | undefined): Maturity {
  let best: Maturity = 'planned';
  for (const c of adapter?.capabilities ?? []) if (RANK[c.maturity] > RANK[best]) best = c.maturity;
  return best;
}

/** A planned adapter has no code: testing or invoking it is refused by the server (422 adapter_not_implemented). */
export const isPlannedAdapter = (adapter: Pick<Adapter, 'capabilities'> | undefined): boolean =>
  adapterMaturity(adapter) === 'planned';

export const PLANNED_EXPLANATION =
  'This adapter is declared only: no adapter code exists, so it cannot be tested or invoked.';
