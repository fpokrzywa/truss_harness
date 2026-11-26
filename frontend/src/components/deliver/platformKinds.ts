import type {
  BindingsOf,
  PlatformBindings,
  PlatformKind,
  TargetOut,
  UnusableBindings,
} from '@/lib/api/delivery-types';

/** One binding field of the register form and its label in the targets list. */
export interface BindingField {
  key: string;
  label: string;
  maxLength?: number;
  optional?: boolean;
  help?: string;
  /** A checkbox, sent as a boolean; its form label differs from the list label. */
  checkbox?: { label: string; help: string };
  /** The message for a non-empty, trimmed value that the server would refuse. */
  check?: (value: string, all: Record<string, string>) => string | undefined;
}

interface KindSpec<K extends PlatformKind> {
  /** Only required, unless `check` mirrors the server's rule for the kind. */
  address: { label: string; help: string; check?: (value: string) => string | undefined };
  fields: BindingField[];
  /** The bindings from the trimmed form values; a checkbox reads `'true'` or `''`. */
  bindings: (v: Record<string, string>) => BindingsOf<K>;
  /** The shown values of stored bindings, in the order of `fields`. */
  show: (b: BindingsOf<K>) => string[];
  /** Shown on every plan of a target of this kind. */
  planNote?: string;
}

const rule =
  (pattern: RegExp, message: string) =>
  (value: string): string | undefined =>
    pattern.test(value) ? undefined : message;

const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
const GUID_RULE = 'Use a GUID: 8-4-4-4-12 hexadecimal digits with hyphens.';
const UUID_RULE = 'Use a UUID: 8-4-4-4-12 hexadecimal digits with hyphens.';
const SERVING_ENDPOINT = rule(
  /^[A-Za-z0-9_-]{1,63}$/,
  'Use 1-63 letters, digits, underscores or hyphens.',
);
const SNOWFLAKE_IDENTIFIER = rule(
  /^[A-Za-z_][A-Za-z0-9_$]{0,254}$/,
  'Use 1-255 letters, digits, _ or $, starting with a letter or _.',
);
const FABRIC_ITEM = rule(
  /^[A-Za-z][A-Za-z0-9_]{0,99}$/,
  'Use 1-100 letters, digits or underscores, starting with a letter.',
);
const WAREHOUSE_ID = rule(/^[A-Za-z0-9-]{1,64}$/, 'Use 1-64 letters, digits or hyphens.');

/** A Foundry RID of one resource kind: `ri.<service>.<instance>.<kind>.<uuid>`. */
const rid = (kind: string, what: string) =>
  rule(
    new RegExp(`^ri\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.${kind}\\.${UUID}$`),
    `Use the RID of the ${what}: ri.<service>.<instance>.${kind}.<id>.`,
  );

function port(value: string): string | undefined {
  const n = Number(value);
  return /^\d+$/.test(value) && n >= 1024 && n <= 65535
    ? undefined
    : 'Use a whole number from 1024 to 65535.';
}

/** Every platform kind: its address, binding fields and how its stored bindings are shown. */
export const PLATFORM_KINDS: { [K in PlatformKind]: KindSpec<K> } = {
  databricks: {
    address: { label: 'Address', help: 'The Databricks workspace URL.' },
    fields: [
      { key: 'warehouse_id', label: 'Warehouse ID', maxLength: 64 },
      {
        key: 'serving_endpoint',
        label: 'Serving endpoint',
        maxLength: 63,
        optional: true,
        help: 'The model serving endpoint a generated app queries. Needed to deploy a generated app.',
        check: SERVING_ENDPOINT,
      },
    ],
    bindings: (v) => ({
      warehouse_id: v.warehouse_id ?? '',
      ...(v.serving_endpoint ? { serving_endpoint: v.serving_endpoint } : {}),
    }),
    show: (b) => [b.warehouse_id, b.serving_endpoint || '-'],
  },
  snowflake: {
    address: {
      label: 'Account address',
      help: 'The Snowflake account URL: https://<account>.snowflakecomputing.com',
    },
    fields: [
      { key: 'package', label: 'Package', maxLength: 255, check: SNOWFLAKE_IDENTIFIER },
      { key: 'application', label: 'Application', maxLength: 255, check: SNOWFLAKE_IDENTIFIER },
      { key: 'warehouse', label: 'Warehouse', maxLength: 255, check: SNOWFLAKE_IDENTIFIER },
      {
        key: 'cortex_model',
        label: 'Cortex model',
        maxLength: 64,
        check: rule(
          /^[a-z0-9][a-z0-9._-]{0,63}$/,
          'Use 1-64 lowercase letters, digits, dots, underscores or hyphens, starting with a letter or digit.',
        ),
      },
    ],
    bindings: (v) => ({
      package: v.package ?? '',
      application: v.application ?? '',
      warehouse: v.warehouse ?? '',
      cortex_model: v.cortex_model ?? '',
    }),
    show: (b) => [b.package, b.application, b.warehouse, b.cortex_model],
  },
  fabric: {
    address: {
      label: 'Fabric workspace',
      help: 'The workspace ID, or its URL: https://app.fabric.microsoft.com/groups/<workspace id>',
    },
    fields: [
      {
        key: 'entra_tenant_id',
        label: 'Entra tenant ID',
        maxLength: 64,
        check: rule(new RegExp(`^${UUID}$`), GUID_RULE),
      },
      { key: 'lakehouse_name', label: 'Lakehouse name', maxLength: 100, check: FABRIC_ITEM },
      {
        key: 'notebook_name',
        label: 'Notebook name',
        maxLength: 100,
        check: (value, all) =>
          FABRIC_ITEM(value) ??
          (value === all.lakehouse_name
            ? 'The lakehouse and the notebook need different names.'
            : undefined),
      },
    ],
    bindings: (v) => ({
      entra_tenant_id: v.entra_tenant_id ?? '',
      lakehouse_name: v.lakehouse_name ?? '',
      notebook_name: v.notebook_name ?? '',
    }),
    show: (b) => [b.entra_tenant_id, b.lakehouse_name, b.notebook_name],
  },
  palantir: {
    address: { label: 'Foundry stack address', help: 'https://<stack>.palantirfoundry.com' },
    fields: [
      {
        key: 'marketplace_rid',
        label: 'Marketplace RID',
        maxLength: 255,
        check: rid('local', 'Marketplace'),
      },
      {
        key: 'product_id',
        label: 'Product ID',
        maxLength: 64,
        check: rule(new RegExp(`^${UUID}$`), UUID_RULE),
      },
      {
        key: 'version_id',
        label: 'Version ID',
        maxLength: 64,
        check: rule(new RegExp(`^${UUID}$`), UUID_RULE),
      },
      {
        key: 'namespace_rid',
        label: 'Namespace RID',
        maxLength: 255,
        check: rid('namespace', 'namespace'),
      },
      {
        key: 'parent_folder_rid',
        label: 'Parent folder RID',
        maxLength: 255,
        check: rid('folder', 'parent folder'),
      },
      {
        key: 'ontology_rid',
        label: 'Ontology RID',
        maxLength: 255,
        check: rid('ontology', 'ontology'),
      },
      {
        key: 'object_type',
        label: 'Object type',
        maxLength: 128,
        check: rule(
          /^[A-Za-z][A-Za-z0-9_.]{0,127}$/,
          'Use 1-128 letters, digits, underscores or dots, starting with a letter.',
        ),
      },
      {
        key: 'frontend_origin',
        label: 'Frontend origin',
        maxLength: 2048,
        check: (value) => (value.startsWith('https://') ? undefined : 'Use an https:// origin.'),
      },
      {
        key: 'frontend_asset_sha256',
        label: 'Frontend asset SHA-256',
        maxLength: 128,
        check: rule(/^[a-f0-9]{64}$/, 'Use 64 lowercase hexadecimal digits.'),
      },
      {
        key: 'allow_preview_apis',
        label: 'Preview APIs',
        checkbox: {
          label: 'Allow Foundry preview APIs',
          help: 'Palantir installs use Foundry preview APIs. Without this, every plan on this target is refused.',
        },
      },
    ],
    bindings: (v) => ({
      marketplace_rid: v.marketplace_rid ?? '',
      product_id: v.product_id ?? '',
      version_id: v.version_id ?? '',
      namespace_rid: v.namespace_rid ?? '',
      parent_folder_rid: v.parent_folder_rid ?? '',
      ontology_rid: v.ontology_rid ?? '',
      object_type: v.object_type ?? '',
      frontend_origin: v.frontend_origin ?? '',
      frontend_asset_sha256: v.frontend_asset_sha256 ?? '',
      allow_preview_apis: v.allow_preview_apis === 'true',
    }),
    show: (b) => [
      b.marketplace_rid,
      b.product_id,
      b.version_id,
      b.namespace_rid,
      b.parent_folder_rid,
      b.ontology_rid,
      b.object_type,
      b.frontend_origin,
      b.frontend_asset_sha256,
      b.allow_preview_apis ? 'Allowed' : 'Not allowed',
    ],
    planNote:
      "The installed product's content is not derived from the release. SETH records which release authorized the install; it does not check the product's content against it.",
  },
  docker: {
    address: {
      label: 'Docker daemon address',
      help: "docker://<daemon id>. The id is the output of docker info --format '{{.ID}}' on the host: a lowercase UUID, or twelve groups of four capital letters A-Z or digits 2-7 joined by colons.",
      // contracts.DOCKER_HOST: never case-folded, as the server reads it
      check: rule(
        /^docker:\/\/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[A-Z2-7]{4}(?::[A-Z2-7]{4}){11})$/,
        'Use docker:// and the daemon id: a lowercase UUID, or twelve groups of four capital letters A-Z or digits 2-7 joined by colons.',
      ),
    },
    fields: [
      {
        key: 'project',
        label: 'Project',
        maxLength: 40,
        check: rule(
          /^[a-z][a-z0-9-]{0,39}$/,
          'Use 1-40 lowercase letters, digits or hyphens, starting with a letter.',
        ),
      },
      { key: 'port', label: 'Port', maxLength: 10, check: port },
      {
        key: 'workspace_host',
        label: 'Databricks workspace',
        maxLength: 2048,
        check: rule(
          /^https:\/\/[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$/,
          'Use the workspace URL: https:// and a lowercase host name, with no path.',
        ),
      },
      { key: 'warehouse_id', label: 'Warehouse ID', maxLength: 64, check: WAREHOUSE_ID },
      {
        key: 'serving_endpoint',
        label: 'Serving endpoint',
        maxLength: 63,
        check: SERVING_ENDPOINT,
      },
    ],
    bindings: (v) => ({
      project: v.project ?? '',
      port: Number(v.port),
      workspace_host: v.workspace_host ?? '',
      warehouse_id: v.warehouse_id ?? '',
      serving_endpoint: v.serving_endpoint ?? '',
    }),
    show: (b) => [b.project, String(b.port), b.workspace_host, b.warehouse_id, b.serving_endpoint],
  },
};

/** The kinds in the order the register form offers them. */
export const PLATFORMS = Object.keys(PLATFORM_KINDS) as PlatformKind[];

/** False for a kind this screen does not know (a newer server); it gets a neutral rendering. */
export const isKnownKind = (kind: string): kind is PlatformKind =>
  Object.hasOwn(PLATFORM_KINDS, kind);

export const isUnusable = (b: TargetOut['bindings']): b is UnusableBindings => 'unusable' in b;

function showBindings<K extends PlatformKind>(kind: K, b: BindingsOf<K>): string[] {
  return PLATFORM_KINDS[kind].show(b);
}

/** The labelled bindings of a usable target of a known kind, in the order of its fields. */
export function bindingRows(t: TargetOut): [label: string, value: string][] | null {
  if (!isKnownKind(t.platform_kind) || isUnusable(t.bindings)) return null;
  const values = showBindings(t.platform_kind, t.bindings);
  return PLATFORM_KINDS[t.platform_kind].fields.map((f, i) => [f.label, values[i] ?? '-']);
}

/** The register body's kind and bindings from the trimmed form values. */
export function platformBindings<K extends PlatformKind>(
  kind: K,
  values: Record<string, string>,
): PlatformBindings {
  return {
    platform_kind: kind,
    bindings: PLATFORM_KINDS[kind].bindings(values),
  } as PlatformBindings;
}

/** The note shown on every plan of a target of this kind, if any. */
export function planNote(kind: string): string | undefined {
  return isKnownKind(kind) ? PLATFORM_KINDS[kind].planNote : undefined;
}
