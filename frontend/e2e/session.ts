import type { Page } from '@playwright/test';
import { passwordFor } from './users';

export const EMAIL = 'admin@demo-a.local';

/**
 * Signs in through the /login form. `/` is still the shell placeholder, so the run enters through
 * the org's command page: signed out, the app sends it to `/login?next=...` and back after sign-in.
 */
export async function signIn(page: Page): Promise<void> {
  await page.goto('/o/demo-a/command');
  await page.waitForURL('**/login?next=*');
  await page.getByLabel('Email', { exact: true }).fill(EMAIL);
  await page.getByLabel('Password', { exact: true }).fill(passwordFor(EMAIL));
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.waitForURL('**/o/demo-a/command');
}
