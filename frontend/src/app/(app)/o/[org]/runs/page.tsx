import type { Metadata } from 'next';
import { RunHistoryView } from '@/components/operate/RunHistoryView';

export const metadata: Metadata = { title: 'Runs' };

export default function Page() {
  return <RunHistoryView />;
}
