import type { Metadata } from 'next';
import { PlatformIdpsView } from '@/components/security/IdpsView';

export const metadata: Metadata = { title: 'Platform identity providers' };

export default function Page() {
  return <PlatformIdpsView />;
}
