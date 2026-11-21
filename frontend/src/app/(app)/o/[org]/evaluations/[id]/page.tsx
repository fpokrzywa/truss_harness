import type { Metadata } from 'next';
import { EvaluationRunView } from '@/components/releases/EvaluationRunView';

export const metadata: Metadata = { title: 'Evaluation run' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EvaluationRunView id={id} />;
}
