import type { TargetOut } from '@/lib/api/delivery-types';

/** The target's name from the first page of targets, or its id when it is not there. */
export function targetName(targets: TargetOut[] | undefined, id: string): string {
  return targets?.find((t) => t.id === id)?.name ?? id;
}
