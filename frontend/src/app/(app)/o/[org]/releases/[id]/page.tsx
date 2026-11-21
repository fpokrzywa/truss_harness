import type { Metadata } from 'next';
import { ReleaseDetailView } from '@/components/releases/ReleaseDetailView';

export const metadata: Metadata = { title: 'Release' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ReleaseDetailView id={id} />;
}
