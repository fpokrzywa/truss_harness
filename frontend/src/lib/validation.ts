/** Slug rule shared with the API (PHASE_1_CONTRACT). */
export const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export function validateSlug(value: string): string | null {
  if (value.length === 0) return 'Slug is required.';
  if (!SLUG_RE.test(value)) {
    return 'Use 1-63 lowercase letters, digits or hyphens; start and end with a letter or digit.';
  }
  return null;
}

export function validateName(value: string): string | null {
  const v = value.trim();
  if (v.length === 0) return 'Name is required.';
  if (v.length > 200) return 'Name must be 200 characters or fewer.';
  return null;
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateEmail(value: string): string | null {
  if (value.trim().length === 0) return 'Email is required.';
  return EMAIL_RE.test(value.trim()) ? null : 'Enter a valid email address.';
}

/** Derive a slug suggestion from a display name. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63)
    .replace(/-+$/g, '');
}

const PLACEHOLDER_ORIGIN = 'http://shifde.invalid';
const UNSAFE_CHARS = /[\x00-\x1F\x7F\\]/;

/**
 * Only same-origin relative paths are valid post-login targets (open-redirect guard).
 * Rejects control characters (URL parsers strip tab/CR/LF, so a tab between two slashes becomes a
 * protocol-relative URL) and backslashes, both raw and percent-decoded, then requires the
 * parsed URL to stay on our origin.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//')) return '/';
  let decoded: string;
  try {
    decoded = decodeURIComponent(next);
  } catch {
    return '/'; // malformed percent-encoding
  }
  if (UNSAFE_CHARS.test(next) || UNSAFE_CHARS.test(decoded) || decoded.startsWith('//')) return '/';
  const origin = typeof window !== 'undefined' ? window.location.origin : PLACEHOLDER_ORIGIN;
  let url: URL;
  try {
    url = new URL(next, origin);
  } catch {
    return '/';
  }
  if (url.origin !== origin) return '/';
  return url.pathname + url.search + url.hash;
}
