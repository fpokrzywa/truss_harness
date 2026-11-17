import type { Metadata } from 'next';
import { RunHistoryView } from '@/components/operate/RunHistoryView';

export const metadata: Metadata = { title: 'Project runs' };

export default function Page() {
  return <RunHistoryView />;
}
