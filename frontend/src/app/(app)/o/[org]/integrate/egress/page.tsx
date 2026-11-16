import type { Metadata } from 'next';
import { EgressView } from '@/components/integrate/EgressView';

export const metadata: Metadata = { title: 'Egress policies' };

export default function Page() {
  return <EgressView />;
}
