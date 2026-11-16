import type { Metadata } from 'next';
import { PlatformRateCardsView } from '@/components/governance/PricingView';

export const metadata: Metadata = { title: 'Global rate cards' };

export default function Page() {
  return <PlatformRateCardsView />;
}
