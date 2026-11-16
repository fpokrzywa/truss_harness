import type { Metadata } from 'next';
import { ApiKeysView } from '@/components/security/ApiKeysView';

export const metadata: Metadata = { title: 'API keys' };

export default function Page() {
  return <ApiKeysView />;
}
