import type { ReactNode } from 'react';
import { FeatureGate } from '@/components/FeatureGate';

export default function Layout({ children }: { children: ReactNode }) {
  return <FeatureGate feature="governance">{children}</FeatureGate>;
}
