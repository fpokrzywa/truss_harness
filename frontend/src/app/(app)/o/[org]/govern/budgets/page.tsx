import type { Metadata } from 'next';
import { BudgetsView } from '@/components/governance/BudgetsView';

export const metadata: Metadata = { title: 'Budgets and quotas' };

export default function Page() {
  return <BudgetsView />;
}
