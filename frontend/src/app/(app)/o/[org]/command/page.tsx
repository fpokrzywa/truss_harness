import type { Metadata } from 'next';
import { CommandCenter } from '@/components/command/CommandCenter';

export const metadata: Metadata = { title: 'Dashboard' };

export default function Page() {
  return <CommandCenter />;
}
