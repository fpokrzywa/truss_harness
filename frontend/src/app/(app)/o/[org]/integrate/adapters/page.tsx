import type { Metadata } from 'next';
import { AdaptersView } from '@/components/integrate/AdaptersView';

export const metadata: Metadata = { title: 'Adapters' };

export default function Page() {
  return <AdaptersView />;
}
