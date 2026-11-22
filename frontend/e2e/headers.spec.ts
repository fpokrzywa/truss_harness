import { expect, test, type Page, type Response } from '@playwright/test';
import { signIn } from './session';

// Go-live design section 5: every page carries a nonce CSP and the other browser headers, and the
// main pages load without a CSP violation or a console error.

const PAGES = [
  '/o/demo-a/command',
  '/o/demo-a/workspaces',
  '/o/demo-a/w/main',
  '/o/demo-a/build/agents',
  '/o/demo-a/govern/overview',
  '/o/demo-a/deliver/plans',
  '/o/demo-a/approvals',
];

type Violation = { page: string; directive: string; blocked: string; source: string };

declare global {
  interface Window {
    __reportCspViolation?: (v: Violation) => void;
  }
}

function expectPageHeaders(response: Response | null, path: string): void {
  expect(response, `no document response for ${path}`).not.toBeNull();
  const headers = response!.headers();
  expect(headers['content-security-policy'], `CSP on ${path}`).toMatch(
    /script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/,
  );
  expect(headers['x-content-type-options'], path).toBe('nosniff');
  expect(headers['referrer-policy'], path).toBe('no-referrer');
  expect(headers['x-frame-options'], path).toBe('DENY');
  expect(headers['cross-origin-opener-policy'], path).toBe('same-origin');
  expect(headers['strict-transport-security'], path).toBeUndefined();
}

/** Chrome's network log line for an API request answered with an HTTP error status. */
function isApiStatusLog(text: string, url: string): boolean {
  return (
    text.startsWith('Failed to load resource: the server responded with a status of 4') &&
    new URL(url, 'http://127.0.0.1').pathname.startsWith('/api/')
  );
}

/** Loads `path` and waits until its data requests have settled. */
async function visit(page: Page, path: string): Promise<Response | null> {
  const response = await page.goto(path, { waitUntil: 'networkidle' });
  await expect(page.locator('main').first()).toBeVisible();
  return response;
}

test('pages carry the security headers and load without CSP violations or console errors', async ({
  page,
}) => {
  const violations: Violation[] = [];
  const errors: string[] = [];
  // The binding outlives each document, so violations on pages the run navigates away from count.
  await page.exposeFunction('__reportCspViolation', (v: Violation) => violations.push(v));
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__reportCspViolation?.({
        page: location.pathname,
        directive: e.violatedDirective,
        blocked: e.blockedURI,
        source: `${e.sourceFile}:${e.lineNumber}`,
      });
    });
  });
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // Chrome logs every 4xx answer as a console error. The app expects some from the API (the
    // signed-out session probe answers 401, a non-platform admin's platform status 403), so only
    // those API answers are skipped; CSP blocks still count above.
    if (isApiStatusLog(msg.text(), msg.location().url)) return;
    errors.push(`${page.url()}: ${msg.text()}`);
  });
  page.on('pageerror', (err) => errors.push(`${page.url()}: ${err.message}`));

  expectPageHeaders(await visit(page, '/login'), '/login');
  await signIn(page);
  for (const path of PAGES) expectPageHeaders(await visit(page, path), path);

  expect(violations, `CSP violations:\n${JSON.stringify(violations, null, 2)}`).toEqual([]);
  expect(errors, `console errors:\n${errors.join('\n')}`).toEqual([]);
});
