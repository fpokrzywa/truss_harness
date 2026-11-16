import type { Metadata } from 'next';
import { VendorDetailView } from '@/components/integrate/VendorDetailView';

export const metadata: Metadata = { title: 'Vendor' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <VendorDetailView id={id} />;
}
