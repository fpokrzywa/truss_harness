import type { Metadata } from 'next';
import { ArtifactListView } from '@/components/build/ArtifactListView';

export const metadata: Metadata = { title: 'Agents' };

export default function Page() {
  return <ArtifactListView kind="agents" />;
}
