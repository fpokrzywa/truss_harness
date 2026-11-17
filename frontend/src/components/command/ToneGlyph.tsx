import type { Tone } from '@/lib/command';

/**
 * Status shape: state is never carried by colour alone, and Pink and Orange are too low-contrast
 * for text (AAA), so they appear only here and on truss lines, always beside a white label.
 */
export function ToneGlyph({ tone, size = 14 }: { tone: Tone; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 14 14"
      width={size}
      height={size}
      className="inline-block shrink-0"
    >
      {tone === 'good' && <rect x="1" y="1" width="12" height="12" className="fill-shi-green" />}
      {tone === 'active' && <path d="M7 0 14 7 7 14 0 7Z" className="fill-shi-pink" />}
      {tone === 'warn' && <path d="M7 0 14 13H0Z" className="fill-shi-orange" />}
      {tone === 'idle' && (
        <rect
          x="2"
          y="2"
          width="10"
          height="10"
          strokeWidth="2"
          className="fill-none stroke-shi-light"
        />
      )}
    </svg>
  );
}
