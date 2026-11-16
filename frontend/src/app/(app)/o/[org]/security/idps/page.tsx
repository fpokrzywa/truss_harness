import type { Metadata } from 'next';
import { IdpsView } from '@/components/security/IdpsView';

export const metadata: Metadata = { title: 'Identity providers' };

export default function Page() {
  return <IdpsView />;
}
