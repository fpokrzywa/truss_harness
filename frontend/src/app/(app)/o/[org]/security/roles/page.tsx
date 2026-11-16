import type { Metadata } from 'next';
import { RolesView } from '@/components/security/RolesView';

export const metadata: Metadata = { title: 'Roles' };

export default function Page() {
  return <RolesView />;
}
