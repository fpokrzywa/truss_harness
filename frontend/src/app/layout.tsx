import type { Metadata } from 'next';
import { Lora, Roboto, Roboto_Mono } from 'next/font/google';
import type { ReactNode } from 'react';
import { Providers } from '@/components/Providers';
import { DEFAULT_PRODUCT_NAME } from '@/lib/product';
import './globals.css';

// next/font downloads these at build time and serves them from this origin: no runtime CDN fetch.
const lora = Lora({ subsets: ['latin'], variable: '--font-cc-lora', display: 'swap' });
const roboto = Roboto({
  subsets: ['latin'],
  weight: ['400', '500', '700'],
  variable: '--font-cc-roboto',
  display: 'swap',
});
const robotoMono = Roboto_Mono({
  subsets: ['latin'],
  variable: '--font-cc-roboto-mono',
  display: 'swap',
});

export const metadata: Metadata = {
  title: { default: DEFAULT_PRODUCT_NAME, template: `%s | ${DEFAULT_PRODUCT_NAME}` },
  description: 'Deploy, monitor and configure enterprise AI system buildouts',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${lora.variable} ${roboto.variable} ${robotoMono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
