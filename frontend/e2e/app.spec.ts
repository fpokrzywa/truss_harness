import { expect, test, type Page } from '@playwright/test';
import { signIn } from './session';

const ORG_A = 'Demo Organization A';
const ORG_B = 'Demo Organization B';

function orgSelect(page: Page) {
  return page.getByRole('group', { name: 'Scope' }).getByLabel('Organization', { exact: true });
}

async function expectOrgSelected(page: Page, name: string): Promise<void> {
  await expect(orgSelect(page).locator('option:checked')).toHaveText(name);
}

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('signs in and lands on the organization command page', async ({ page }) => {
  await expect(page).toHaveURL(/\/o\/demo-a\/command$/);
  await expectOrgSelected(page, ORG_A);
});

test('pins an organization in the scope selector', async ({ page }) => {
  // Start from an org the user is not a member of, so choosing demo-a really navigates.
  await page.goto('/o/demo-b/workspaces');
  await expect(page.getByRole('heading', { name: 'Not found or no access' })).toBeVisible();
  await orgSelect(page).selectOption({ label: ORG_A });
  await expect(page).toHaveURL(/\/o\/demo-a\/workspaces$/);
  await expectOrgSelected(page, ORG_A);

  await page.getByRole('link', { name: 'Main Workspace', exact: true }).click();
  await expect(page).toHaveURL(/\/o\/demo-a\/w\/main$/);
  await expectOrgSelected(page, ORG_A);
});

test('lists the workspaces of the organization', async ({ page }) => {
  await page.goto('/o/demo-a/workspaces');
  await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();
  await expect(page.getByText('1 workspace', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Main Workspace', exact: true })).toBeVisible();
});

test('a demo-a user cannot see demo-b', async ({ page }) => {
  // (a) demo-b is not among the organizations offered.
  await expect(orgSelect(page).getByRole('option', { name: ORG_A, exact: true })).toHaveCount(1);
  await expect(orgSelect(page).getByRole('option', { name: ORG_B, exact: true })).toHaveCount(0);

  // (b) demo-b's workspaces page answers as if it did not exist.
  await page.goto('/o/demo-b/workspaces');
  await expect(page.getByRole('heading', { name: 'Not found or no access' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Main Workspace', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(ORG_B);

  // (c) demo-a's own workspaces page does not mention demo-b.
  await page.goto('/o/demo-a/workspaces');
  await expect(page.getByRole('link', { name: 'Main Workspace', exact: true })).toBeVisible();
  await expect(page.locator('body')).not.toContainText(ORG_B);
});
