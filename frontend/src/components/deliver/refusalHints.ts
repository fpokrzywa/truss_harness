/** Plain words for the plan refusals: 409 codes of the request, and reasons of a failed plan. */
const REFUSAL_HINTS: Record<string, string> = {
  serving_endpoint_required:
    "This release is a generated app, and the target has no serving endpoint. Use a target registered with a serving endpoint, the model serving endpoint named in the release's model binding.",
  serving_endpoint_mismatch:
    "The target's serving endpoint is not the model the release is bound to. Use a target whose serving endpoint matches the release's model binding, or a release bound to that endpoint.",
  model_not_in_target_workspace:
    "The release's model provider is not in the target's Databricks workspace. Use a release whose model provider is in the target's workspace (its address matches the target's address, or a Docker target's Databricks workspace).",
  target_kind_mismatch:
    "The release is built for another platform than the target. Choose a target of the release's platform, or a release built for this target's platform.",
  cortex_model_mismatch:
    "The target's Cortex model is not the model the release is bound to. Use a target whose Cortex model matches the release's model binding, or a release bound to that model.",
  model_not_in_target_account:
    "The release's model provider is not in the target's Snowflake account. Use a release whose model provider is in the target's account (its address matches the target's address).",
  snowflake_patch_limit:
    'This package has no free patch. Register a target with a new package and request the plan again.',
  palantir_preview_not_allowed:
    'This target does not allow Foundry preview APIs, which a Palantir install needs. Register a target with preview APIs allowed, or use one that has them.',
  workspace_not_claimed:
    "The Docker target's Databricks workspace is not registered as a Databricks target of this organization. Register that workspace as a target first, then try again.",
  docker_host_mismatch:
    "The Docker daemon the executor reached is not this target's daemon. Run the executor on the target's host, or register a target with that host's daemon ID, then request the plan again.",
  foreign_project:
    "A compose project with this target's project name exists on the host and does not belong to this target. Register a target with another project name, or remove that project by hand, then request the plan again.",
  foreign_item:
    'The Fabric workspace already has an item with a name this target uses, and SETH did not create it. Register a target with other lakehouse and notebook names, or remove or rename that item, then request the plan again.',
  target_bindings_invalid:
    "This target's stored settings are no longer valid. Archive the target, register it again and request the plan on the new one.",
  plan_stale: 'The target changed after this plan was made. Request a new plan.',
  credential_unavailable:
    "The executor has no usable credential for this target. Check the target's credential reference in the executor's credentials file, then request the plan again.",
  generator_version_unsupported:
    "This release cannot be built for the target's platform. Use a release sealed for that platform.",
  target_unavailable: 'The target is archived. Choose an active target.',
  target_unresolved:
    'A deployment on this target has an unknown outcome. Resolve it on the Deployments screen first.',
  release_withdrawn: 'The release was withdrawn. Choose another release.',
  release_target_mismatch:
    "The release belongs to another workspace or project than the target. Choose a target of the release's workspace and project.",
  release_source_changed:
    'Something the release was built from changed or can no longer be read. Seal a new release.',
  release_not_renderable: 'An app cannot be generated from this release. Seal a new release.',
};

export function refusalHint(code: string): string | undefined {
  return Object.hasOwn(REFUSAL_HINTS, code) ? REFUSAL_HINTS[code] : undefined;
}

const PLAN_FAILED = /^plan job failed: ([a-z_]+)$/;

/** The hint of a failed plan's reason `plan job failed: <code>`, if the code has one. */
export function planFailureHint(reason: string | null): string | undefined {
  const code = reason?.match(PLAN_FAILED)?.[1];
  return code ? refusalHint(code) : undefined;
}
