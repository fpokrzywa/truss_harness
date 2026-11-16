import type { Metadata } from 'next';
import { VendorsView } from '@/components/integrate/VendorsView';

export const metadata: Metadata = { title: 'Vendors' };

export default function Page() {
  return <VendorsView />;
}
