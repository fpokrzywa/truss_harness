/** The secrets service's limit on a stored value, in UTF-8 bytes. */
export const MAX_CREDENTIAL_BYTES = 16384;

/** Client-side check of a credential value; the message never echoes the value. */
export function credentialError(value: string): string | undefined {
  if (value === '') return 'Enter the credential value.';
  if (new TextEncoder().encode(value).length > MAX_CREDENTIAL_BYTES)
    return `The credential must be at most ${MAX_CREDENTIAL_BYTES} bytes.`;
  return undefined;
}
