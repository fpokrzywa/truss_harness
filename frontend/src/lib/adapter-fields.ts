/**
 * Adapter field rules the API does not expose yet (issue #241 moves them into `AdapterOut`).
 *
 * A copy of the `auth_only_fields` and `destination_fields` of the adapter descriptors in
 * `backend/src/shifde/adapters/`: `rest_http.py`, `dataplatforms/databricks.py`,
 * `dataplatforms/snowflake.py`, `mcp_http.py`, `postgres.py` and `model_providers.py`
 * (`openai_compatible`, `anthropic`). The server decides; these only shape the forms.
 */
import type { FieldDef } from './schema-form';

const OAUTH2 = 'oauth2_client_credentials';

/** Config fields that apply only under the listed auth methods, per adapter key. */
export const AUTH_ONLY_FIELDS: Record<string, Record<string, string[]>> = {
  'rest.http': { oauth2_url: [OAUTH2], client_id: [OAUTH2], scope: [OAUTH2] },
  databricks: { client_id: [OAUTH2] },
};

/** Config fields that decide where a bound credential is sent, per adapter key. */
export const DESTINATION_FIELDS: Record<string, string[]> = {
  'rest.http': ['base_url', 'oauth2_url'],
  databricks: ['host'],
  snowflake: ['account'],
  mcp_http: ['url'],
  postgres: ['host', 'port', 'sslmode'],
  openai_compatible: ['base_url'],
  anthropic: ['base_url'],
};

/** The fields shown under `authMethod`: an auth-only field only when that method is listed for it. */
export function visibleConfigFields(
  adapterKey: string,
  authMethod: string,
  fields: FieldDef[],
): FieldDef[] {
  const only = AUTH_ONLY_FIELDS[adapterKey] ?? {};
  return fields.filter((f) => {
    const methods = only[f.key];
    return methods === undefined || methods.includes(authMethod);
  });
}

/** Whether any destination field differs between two configs. */
export function destinationChanged(
  adapterKey: string,
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): boolean {
  return (DESTINATION_FIELDS[adapterKey] ?? []).some(
    (f) => JSON.stringify(before[f]) !== JSON.stringify(after[f]),
  );
}
