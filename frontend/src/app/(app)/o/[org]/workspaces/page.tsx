import type { Metadata } from 'next';
import { WorkspacesView } from '@/components/WorkspacesView';

export const metadata: Metadata = { title: 'Workspaces' };

export default function Page() {
  return <WorkspacesView />;
}
