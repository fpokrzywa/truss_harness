import Link from 'next/link';

/**
 * Division lockup: mark, divider, uppercase name, with equal space either side of the divider.
 * The mark is set in type until the approved SHI logo file is supplied: replace that span only.
 */
export function BrandLockup({ href }: { href?: string }) {
  const lockup = (
    <>
      <span className="text-3xl leading-none font-bold tracking-wide">SHI</span>
      <span aria-hidden="true" className="h-9 w-px bg-shi-light" />
      <span className="text-[0.8125rem] leading-tight font-medium tracking-[0.14em] uppercase sm:text-base sm:tracking-[0.2em]">
        Enterprise Truss Harness
      </span>
    </>
  );
  const layout = 'inline-flex items-center gap-4 text-shi-ink sm:gap-6';
  return href ? (
    <Link href={href} aria-label="SHI Enterprise Truss Harness, home" className={layout}>
      {lockup}
    </Link>
  ) : (
    <span role="img" aria-label="SHI Enterprise Truss Harness" className={layout}>
      {lockup}
    </span>
  );
}
