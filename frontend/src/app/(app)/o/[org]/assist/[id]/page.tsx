import type { Metadata } from 'next';
import { AssistRequestView } from '@/components/assist/AssistRequestView';

export const metadata: Metadata = { title: 'Ask SETH request' };

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AssistRequestView id={id} />;
}
