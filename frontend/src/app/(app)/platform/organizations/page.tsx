import type { Metadata } from 'next';
import { OrganizationsView } from '@/components/OrganizationsView';

export const metadata: Metadata = { title: 'Organizations' };

export default function Page() {
  return <OrganizationsView />;
}
