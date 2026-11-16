import type { Metadata } from 'next';
import { UsersView } from '@/components/security/UsersView';

export const metadata: Metadata = { title: 'Users' };

export default function Page() {
  return <UsersView />;
}
