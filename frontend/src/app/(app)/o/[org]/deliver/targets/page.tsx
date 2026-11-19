import type { Metadata } from 'next';
import { TargetsView } from '@/components/deliver/TargetsView';

export const metadata: Metadata = { title: 'Deployment targets' };

export default function Page() {
  return <TargetsView />;
}
