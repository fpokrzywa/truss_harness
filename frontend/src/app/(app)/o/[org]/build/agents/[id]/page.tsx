import type { Metadata } from 'next';
import { ArtifactDetailView } from '@/components/build/ArtifactDetailView';

export const metadata: Metadata = { title: 'Agents' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ArtifactDetailView kind="agents" id={id} />;
}
