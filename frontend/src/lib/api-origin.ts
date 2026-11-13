export const DEFAULT_API_ORIGIN = 'http://127.0.0.1:8710';

/**
 * Origin of the SHIFDE API that `/api/*` is proxied to. Read from `SHIFDE_API_ORIGIN` on every
 * call so the same build serves any installation: it is a runtime setting of `next start` /
 * `node server.js`, never baked into the build. Throws on an invalid value.
 */
export function apiOrigin(env: Record<string, string | undefined> = process.env): string {
  const raw = env.SHIFDE_API_ORIGIN?.trim() || DEFAULT_API_ORIGIN;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`SHIFDE_API_ORIGIN is not a valid URL: ${raw}`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:')
    throw new Error(`SHIFDE_API_ORIGIN must be http(s): ${raw}`);
  return url.origin;
}
