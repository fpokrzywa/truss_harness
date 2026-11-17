import type { Metadata } from 'next';
import { PlatformStatusView } from '@/components/PlatformStatusView';

export const metadata: Metadata = { title: 'Platform Status' };

export default function Page() {
  return <PlatformStatusView />;
}
