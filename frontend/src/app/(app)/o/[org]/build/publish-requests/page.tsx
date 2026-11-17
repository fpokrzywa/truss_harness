import type { Metadata } from 'next';
import { PublishRequestsView } from '@/components/build/PublishRequestsView';

export const metadata: Metadata = { title: 'Publish Requests' };

export default function Page() {
  return <PublishRequestsView />;
}
