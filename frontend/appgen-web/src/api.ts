// The generated service's routes the renderer calls (design SP5 6.4) and their result shapes.

export type AskResult = { answer: string; tool_calls: unknown[] };
export type ToolColumn = { name: string; type: string };
export type ToolResult = {
  columns: ToolColumn[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
};

export type AppApi = {
  ask(message: string): Promise<AskResult>;
  runTool(tool: string, params: Record<string, string>): Promise<ToolResult>;
};

export class ApiError extends Error {}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError('The request failed.');
  }
  if (!response.ok) throw new ApiError(`The request failed (HTTP ${response.status}).`);
  return (await response.json()) as T;
}

/** The API of the generated service the bundle is served by. */
export const httpApi: AppApi = {
  ask: (message) => postJson<AskResult>('/api/ask', { message }),
  runTool: (tool, params) =>
    postJson<ToolResult>(`/api/tools/${encodeURIComponent(tool)}`, { params }),
};

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : 'The request failed.';
}
