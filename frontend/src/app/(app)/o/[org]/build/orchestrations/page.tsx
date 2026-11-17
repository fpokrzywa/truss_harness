import type { Metadata } from 'next';
import { ArtifactListView } from '@/components/build/ArtifactListView';

export const metadata: Metadata = { title: 'Orchestrations' };

export default function Page() {
  return <ArtifactListView kind="orchestrations" />;
}
