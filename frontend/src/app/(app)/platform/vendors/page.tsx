import type { Metadata } from 'next';
import { PlatformVendorsView } from '@/components/integrate/VendorsView';

export const metadata: Metadata = { title: 'Global vendors' };

export default function Page() {
  return <PlatformVendorsView />;
}
