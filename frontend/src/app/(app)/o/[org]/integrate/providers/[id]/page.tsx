import type { Metadata } from 'next';
import { ProviderDetailView } from '@/components/integrate/ProviderDetailView';

export const metadata: Metadata = { title: 'Model provider' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ProviderDetailView id={id} />;
}
