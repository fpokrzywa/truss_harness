import type { Metadata } from 'next';
import { AuditView } from '@/components/security/AuditView';

export const metadata: Metadata = { title: 'Security audit' };

export default function Page() {
  return <AuditView />;
}
