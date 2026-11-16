import type { Metadata } from 'next';
import { DashboardView } from '@/components/DashboardView';

export const metadata: Metadata = { title: 'Organization command center' };

export default function Page() {
  return <DashboardView />;
}
