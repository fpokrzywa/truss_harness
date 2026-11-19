import type { Metadata } from 'next';
import { DeploymentDetailView } from '@/components/deliver/DeploymentDetailView';

export const metadata: Metadata = { title: 'Deployment' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DeploymentDetailView id={id} />;
}
