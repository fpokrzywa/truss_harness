import type { Metadata } from 'next';
import { ExecutorsView } from '@/components/deliver/ExecutorsView';

export const metadata: Metadata = { title: 'Executors' };

export default function Page() {
  return <ExecutorsView />;
}
