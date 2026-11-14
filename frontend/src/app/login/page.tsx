import type { Metadata } from 'next';
import { Suspense } from 'react';
import { AppBrand } from '@/components/AppBrand';
import { LoginForm } from '@/components/LoginForm';

export const metadata: Metadata = { title: 'Sign in' };

export default function LoginPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-16 p-6">
      <AppBrand lockupOnly />
      <Suspense fallback={<p role="status">Loading...</p>}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
