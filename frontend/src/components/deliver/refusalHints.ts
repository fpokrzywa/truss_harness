/** Plain words for the plan refusals: 409 codes of the request, and reasons of a failed plan. */
const REFUSAL_HINTS: Record<string, string> = {
  serving_endpoint_required:
    "This release is a generated app, and the target has no serving endpoint. Use a target registered with a serving endpoint, the model serving endpoint named in the release's model binding.",
  serving_endpoint_mismatch:
    "The target's serving endpoint is not the model the release is bound to. Use a target whose serving endpoint matches the release's model binding, or a release bound to that endpoint.",
  model_not_in_target_workspace:
    "The release's model provider is not in the target's Databricks workspace. Use a release whose model provider is in the target's workspace (its address matches the target's address).",
  target_kind_mismatch:
    "The release is built for another platform than the target. Choose a target of the release's platform (Databricks or Snowflake), or a release built for this target's platform.",
  cortex_model_mismatch:
    "The target's Cortex model is not the model the release is bound to. Use a target whose Cortex model matches the release's model binding, or a release bound to that model.",
  model_not_in_target_account:
    "The release's model provider is not in the target's Snowflake account. Use a release whose model provider is in the target's account (its address matches the target's address).",
  snowflake_patch_limit:
    'This package has no free patch. Register a target with a new package and request the plan again.',
};

export function refusalHint(code: string): string | undefined {
  return Object.hasOwn(REFUSAL_HINTS, code) ? REFUSAL_HINTS[code] : undefined;
}
