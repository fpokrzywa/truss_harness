import type { Metadata } from 'next';
import { PlatformEgressView } from '@/components/integrate/EgressView';

export const metadata: Metadata = { title: 'Default egress policy' };

export default function Page() {
  return <PlatformEgressView />;
}
