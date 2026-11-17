import type { Metadata } from 'next';
import { ArtifactListView } from '@/components/build/ArtifactListView';

export const metadata: Metadata = { title: 'Pipelines' };

export default function Page() {
  return <ArtifactListView kind="pipelines" />;
}
