import type { ApiClient } from './client';

const enc = encodeURIComponent;
const mcp$ = (org: string) => `/api/v1/orgs/${enc(org)}/mcp/settings`;

/** The v1 MCP tools, in the order the settings screen lists them. */
export const MCP_TOOLS = [
  'whoami',
  'list_projects',
  'list_agents',
  'start_run',
  'get_run',
  'list_releases',
  'get_release',
  'list_deployments',
  'get_deployment',
] as const;

/** `allowed_tools: null` means this set: every v1 tool except the one that spends. */
export const MCP_DEFAULT_TOOLS: readonly string[] = MCP_TOOLS.filter((t) => t !== 'start_run');

export interface McpSettings {
  enabled: boolean;
  rate_per_minute: number;
  max_cost_per_call: number;
  allowed_tools: string[] | null;
  updated_by: string | null;
  updated_at: string | null;
  endpoint_url: string | null;
}

export type McpSettingsInput = Pick<
  McpSettings,
  'enabled' | 'rate_per_minute' | 'max_cost_per_call' | 'allowed_tools'
>;

/** The organization's MCP settings (`shifde.mcp_access.api`). */
export function createMcpEndpoints(client: ApiClient) {
  return {
    getMcpSettings: (org: string) => client.request<McpSettings>(mcp$(org)),
    putMcpSettings: (org: string, input: McpSettingsInput) =>
      client.request<McpSettings>(mcp$(org), { method: 'PUT', body: input }),
  };
}
