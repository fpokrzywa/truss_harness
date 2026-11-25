const LABELS: Record<string, string> = { databricks: 'Databricks', snowflake: 'Snowflake' };

/** The display name of a data platform (an adapter key or a target's platform kind). */
export function platformLabel(platform: string): string {
  return Object.hasOwn(LABELS, platform) ? LABELS[platform]! : platform;
}
