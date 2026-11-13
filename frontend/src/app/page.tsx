import { DEFAULT_PRODUCT_NAME } from '@/lib/product';

export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-3 p-8">
      <h1 className="text-3xl font-semibold text-slate-900">{DEFAULT_PRODUCT_NAME}</h1>
      <p role="status" className="text-slate-600">
        Front-end shell is running. Module pages arrive as each backend module lands.
      </p>
    </main>
  );
}
