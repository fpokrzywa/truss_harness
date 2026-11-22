import { redirect } from 'next/navigation';
import { connectionsHref } from '@/lib/scope';

/** CM-6: the list moved to the Connections space. */
export default async function Page({ params }: { params: Promise<{ org: string }> }) {
  const { org } = await params;
  redirect(connectionsHref(decodeURIComponent(org)));
}
