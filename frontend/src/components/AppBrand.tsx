'use client';

import { BrandLockup } from './command/BrandLockup';

/**
 * The header brand: the SHI lockup. SETH switched to the installation's own name and logo when a
 * branding profile was set; that belongs to the `installation` area and is not ported here.
 * `lockupOnly` is kept so the call sites in AppShell and the sign-in page stay unchanged.
 */
export function AppBrand({ href }: { href?: string; lockupOnly?: boolean }) {
  return <BrandLockup href={href} />;
}
