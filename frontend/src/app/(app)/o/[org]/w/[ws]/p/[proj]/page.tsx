import type { Metadata } from 'next';
import { DashboardView } from '@/components/DashboardView';

export const metadata: Metadata = { title: 'Project command center' };

export default function Page() {
  return <DashboardView />;
}
