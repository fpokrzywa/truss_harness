import { redirect } from 'next/navigation';
import { connectionHref } from '@/lib/scope';

/** CM-6: connection pages moved under the Connections space. */
export default async function Page({ params }: { params: Promise<{ org: string; id: string }> }) {
  const { org, id } = await params;
  redirect(connectionHref(decodeURIComponent(org), decodeURIComponent(id), 'tests'));
}
