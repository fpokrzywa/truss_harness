import type { Metadata } from 'next';
import { PermissionsView } from '@/components/security/PermissionsView';

export const metadata: Metadata = { title: 'Permissions' };

export default function Page() {
  return <PermissionsView />;
}
