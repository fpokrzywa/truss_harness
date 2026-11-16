import type { Metadata } from 'next';
import { SecretsView } from '@/components/security/SecretsView';

export const metadata: Metadata = { title: 'Secrets' };

export default function Page() {
  return <SecretsView />;
}
