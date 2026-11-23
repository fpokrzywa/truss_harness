import { expect, test, type Page } from '@playwright/test';
import { signIn } from './session';

// The API and worker run with SHIFDE_DEV_FAKE_DATA_PLATFORMS=true (playwright.config.ts), so this
// Databricks host is served by the in-process fake workspace: any non-empty bearer token works.
const HOST = 'adb-1234567890123456.7.azuredatabricks.net';
const TEST_TIMEOUT = 90_000;

async function next(page: Page) {
  await page.getByRole('dialog').getByRole('button', { name: 'Next', exact: true }).click();
}

/** Runs the queued test from the section `region` and waits for the worker's result there. */
async function runTest(page: Page, region: string, button: string) {
  const section = page.getByRole('region', { name: region, exact: true });
  await section.getByRole('button', { name: button, exact: true }).click();
  await expect(section.getByRole('table', { name: 'Checks for this test run' })).toBeVisible({
    timeout: 70_000,
  });
}

test('creates, tests, discovers, edits, rotates and retires a Databricks connection', async ({
  page,
}) => {
  test.setTimeout(240_000);
  const name = `e2e databricks ${Date.now()}`;
  await signIn(page);
  await page.goto('/o/demo-a/connections');
  await page.getByRole('button', { name: 'Create connection', exact: true }).click();
  const wizard = page.getByRole('dialog');

  // Kind
  await wizard.getByLabel('Name', { exact: true }).fill(name);
  await wizard.getByLabel('Adapter', { exact: true }).selectOption('databricks');
  await wizard.getByLabel('Connection type', { exact: true }).selectOption('data_platform');
  await wizard.getByLabel('Authentication method', { exact: true }).selectOption('bearer');
  await next(page);
  // Scope
  await wizard.getByLabel('Workspace', { exact: true }).selectOption('main');
  await wizard.getByLabel('Project', { exact: true }).selectOption('sample');
  await next(page);
  // Configuration
  await wizard.getByLabel('Workspace URL (required)', { exact: true }).fill(HOST);
  await next(page);
  // Credential
  await wizard.getByLabel('Credential value', { exact: true }).fill('dapi-e2e-token-1');
  await next(page);
  // Access
  await wizard.getByLabel('I confirm this credential is read-only', { exact: true }).check();
  await next(page);
  // Review
  await expect(wizard.getByText('Set (hidden)', { exact: true })).toBeVisible();
  await expect(wizard).not.toContainText('dapi-e2e-token-1');
  await wizard.getByRole('button', { name: 'Create connection', exact: true }).click();

  await page.waitForURL(/\/o\/demo-a\/connections\/[^/]+$/);
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();

  await test.step('test', async () => {
    await runTest(page, 'Test', 'Test connection');
  });

  await test.step('discover', async () => {
    await page.getByRole('button', { name: 'Run discovery', exact: true }).click();
    const catalogs = page.getByRole('list', { name: 'Catalogs' });
    await expect(catalogs).toBeVisible({ timeout: TEST_TIMEOUT });
    await expect(catalogs.getByText('main', { exact: true })).toBeVisible();
    await catalogs.getByText('main', { exact: true }).click();
    await expect(page.getByRole('list', { name: 'Schemas in main' })).toBeVisible();
  });

  await test.step('edit a non-destination field', async () => {
    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    const edit = page.getByRole('dialog');
    await edit.getByLabel('Maximum rows per statement', { exact: true }).fill('500');
    await expect(edit.getByText(/sent to the new destination/)).toHaveCount(0);
    await edit.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('500', { exact: true })).toBeVisible();
  });

  await test.step('rotate the credential and test now', async () => {
    await page.getByRole('button', { name: 'Rotate credential', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const input = dialog.getByLabel('New credential value', { exact: true });
    await expect(input).toHaveValue('');
    await expect(input).toHaveAttribute('type', 'password');
    await input.fill('dapi-e2e-token-2');
    await dialog.getByLabel('I confirm this credential is read-only', { exact: true }).check();
    await dialog.getByRole('button', { name: 'Rotate credential', exact: true }).click();
    await expect(page.getByText(/The credential was saved/)).toBeVisible();
    await runTest(page, 'Credential', 'Test now');
  });

  await test.step('retire', async () => {
    await page.getByRole('button', { name: 'Retire', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('Its own credential is disabled');
    await dialog.getByRole('button', { name: 'Retire connection', exact: true }).click();
    await expect(page.getByText('Retired connections cannot change status.')).toBeVisible();
    await expect(page.getByText('retired', { exact: true })).toBeVisible();
  });
});
