// The screens of a generated app, as written to config/screens.json (design SP5 6.3, 6.5).

export type ChatScreen = { kind: 'chat'; title: string; placeholder?: string };
export type TableScreen = { kind: 'table'; title: string; tool: string; inputs: string[] };
export type Screen = ChatScreen | TableScreen;

export const MAX_SCREENS = 6;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseScreen(value: unknown): Screen | null {
  if (!isRecord(value) || typeof value.title !== 'string') return null;
  if (value.kind === 'chat') {
    const { placeholder } = value;
    if (placeholder !== undefined && typeof placeholder !== 'string') return null;
    return { kind: 'chat', title: value.title, placeholder };
  }
  if (value.kind === 'table') {
    const { tool, inputs } = value;
    if (typeof tool !== 'string') return null;
    if (!Array.isArray(inputs) || !inputs.every((name) => typeof name === 'string')) return null;
    return { kind: 'table', title: value.title, tool, inputs: inputs as string[] };
  }
  return null;
}

/** The screens list, or null when `value` is not an array of 1 to 6 valid screens. */
export function parseScreens(value: unknown): Screen[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_SCREENS) return null;
  const screens: Screen[] = [];
  for (const item of value) {
    const screen = parseScreen(item);
    if (screen === null) return null;
    screens.push(screen);
  }
  return screens;
}
