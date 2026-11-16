import type { Metadata } from 'next';
import { ModelDetailView } from '@/components/integrate/ModelDetailView';

export const metadata: Metadata = { title: 'Model' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ModelDetailView id={id} />;
}
