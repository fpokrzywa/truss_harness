import { readFileSync } from 'node:fs';
import path from 'node:path';

/** Written by `shifde-admin dev-seed` (gitignored); `email<TAB>password` lines. */
export const USERS_FILE = path.resolve(
  import.meta.dirname,
  '..',
  '..',
  '.devdata',
  'dev-users.txt',
);

/** Same rules as `read_users` in scripts/two_org_proof.py. */
export function readUsers(file: string = USERS_FILE): Map<string, string> {
  let text: string;
  try {
    text = readFileSync(file, 'utf8');
  } catch {
    throw new Error(`${file} not found: run \`shifde-admin dev-seed\` first`);
  }
  const users = new Map<string, string>();
  for (const line of text.replace(/^﻿/, '').split(/\r?\n/)) {
    if (line.trimStart().startsWith('#')) continue;
    const match = /^\s*(\S+)\s+(.*)$/.exec(line);
    const email = match?.[1];
    const secret = match?.[2]?.trim();
    if (email && secret) users.set(email.toLowerCase(), secret);
  }
  return users;
}

/** The dev password of `email`; never log or attach it. */
export function passwordFor(email: string): string {
  const secret = readUsers().get(email.toLowerCase());
  if (secret === undefined) throw new Error(`${email} is not in ${USERS_FILE}`);
  return secret;
}
