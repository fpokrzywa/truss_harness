import type { Metadata } from 'next';
import { ArtifactDetailView } from '@/components/build/ArtifactDetailView';

export const metadata: Metadata = { title: 'Tools' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ArtifactDetailView kind="tools" id={id} />;
}
