import type { Metadata } from 'next';
import { PlanDetailView } from '@/components/deliver/PlanDetailView';

export const metadata: Metadata = { title: 'Deployment plan' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <PlanDetailView id={id} />;
}
