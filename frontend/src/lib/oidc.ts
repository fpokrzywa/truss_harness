import { safeNextPath } from './validation';

/**
 * Link target for an OIDC provider button. `next` is validated with safeNextPath (open-redirect
 * guard); the default landing page `/` is omitted because the server already uses it.
 */
export function oidcLoginHref(loginUrl: string, next: string | null | undefined): string {
  const safe = safeNextPath(next);
  if (safe === '/') return loginUrl;
  return `${loginUrl}${loginUrl.includes('?') ? '&' : '?'}next=${encodeURIComponent(safe)}`;
}
