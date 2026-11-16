import type { Metadata } from 'next';
import { ConnectionDetailView } from '@/components/integrate/ConnectionDetailView';

export const metadata: Metadata = { title: 'Connection' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ConnectionDetailView id={id} />;
}
