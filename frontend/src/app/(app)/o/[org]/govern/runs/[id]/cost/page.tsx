import type { Metadata } from 'next';
import { RunCostView } from '@/components/governance/RunCostView';

export const metadata: Metadata = { title: 'Workflow cost detail' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunCostView id={id} />;
}
