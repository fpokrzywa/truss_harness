import type { Metadata } from 'next';
import { Suspense } from 'react';
import { AppBrand } from '@/components/AppBrand';
import { SetupForm } from '@/components/SetupForm';

export const metadata: Metadata = { title: 'Set up your account' };

export default function SetupPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-16 p-6">
      <AppBrand lockupOnly />
      <Suspense fallback={<p role="status">Loading...</p>}>
        <SetupForm />
      </Suspense>
    </main>
  );
}
