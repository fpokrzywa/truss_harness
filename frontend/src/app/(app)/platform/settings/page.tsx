import type { Metadata } from 'next';
import { PlatformSettingsView } from '@/components/PlatformSettingsView';

export const metadata: Metadata = { title: 'Platform Settings' };

export default function Page() {
  return <PlatformSettingsView />;
}
