import type { Metadata } from 'next';
import { ConnectionTestsView } from '@/components/integrate/ConnectionTests';

export const metadata: Metadata = { title: 'Connection test results' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ConnectionTestsView id={id} />;
}
