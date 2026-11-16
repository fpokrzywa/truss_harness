import type { Metadata } from 'next';
import { SecurityHome } from '@/components/security/SecurityHome';

export const metadata: Metadata = { title: 'Security' };

export default function Page() {
  return <SecurityHome />;
}
