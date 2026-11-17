import type { Metadata } from 'next';
import { ArtifactDetailView } from '@/components/build/ArtifactDetailView';

export const metadata: Metadata = { title: 'Ontology lineage' };

export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ lineage_id?: string }>;
}) {
  const { id } = await params;
  const { lineage_id: lineageId } = await searchParams;
  return (
    <ArtifactDetailView
      kind="ontologies"
      id={id}
      initialTab="lineage"
      lineageHighlight={lineageId}
    />
  );
}
