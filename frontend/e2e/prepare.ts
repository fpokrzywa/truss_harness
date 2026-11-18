// Prepares this checkout's database before `playwright test` starts the API (`pnpm e2e`).
// Run by Node directly (type stripping), so it imports only Node built-ins.
// Child output is shown with every URL redacted: connection strings and passwords are never printed.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..', '..');
const backend = path.join(root, 'backend');
const usersFile = path.join(root, '.devdata', 'dev-users.txt');

const env: NodeJS.ProcessEnv = { ...process.env, SHIFDE_ENV: 'development', UV_LINK_MODE: 'copy' };
// The checkout's .devdata/dev.env decides, never an inherited URL (as `factory/bin/jobdb ensure`).
delete env.SHIFDE_DATABASE_URL;
delete env.SHIFDE_MIGRATION_DATABASE_URL;

function redact(text: string): string {
  return text.replace(/[a-z][a-z0-9+.-]*:\/\/\S+/gi, '<url redacted>');
}

function run(step: string, command: string, args: string[], cwd: string): void {
  const res = spawnSync(command, args, { cwd, env, encoding: 'utf8' });
  const output = redact(`${res.stdout ?? ''}${res.stderr ?? ''}`).trim();
  if (output) console.log(output);
  if (res.status !== 0) {
    const reason = res.error ? res.error.message : `exit ${res.status ?? 'signal'}`;
    console.error(`e2e prepare: step "${step}" failed (${reason})`);
    process.exit(1);
  }
}

// Keep existing dev passwords: dev-seed then leaves the users file unchanged.
const seedArgs = existsSync(usersFile) ? ['--reuse-passwords'] : [];

run('python scripts/dev_db.py env', 'python', ['scripts/dev_db.py', 'env'], root);
run('shifde-admin db upgrade', 'uv', ['run', '--frozen', 'shifde-admin', 'db', 'upgrade'], backend);
run(
  'shifde-admin dev-seed',
  'uv',
  ['run', '--frozen', 'shifde-admin', 'dev-seed', ...seedArgs],
  backend,
);
