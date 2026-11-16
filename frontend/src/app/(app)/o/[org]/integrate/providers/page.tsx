import type { Metadata } from 'next';
import { ProvidersView } from '@/components/integrate/ProvidersView';

export const metadata: Metadata = { title: 'Model providers' };

export default function Page() {
  return <ProvidersView />;
}
