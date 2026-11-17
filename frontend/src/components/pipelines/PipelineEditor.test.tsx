import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ValidationResult } from '@/lib/api/build-types';
import type { PipelineSpec } from '@/lib/api/pipeline-types';
import { emptyPipelineSpec, pipelineBlockers } from '@/lib/pipeline-spec';
import { artifact } from '@/test/build';
import { stubPipelines } from '@/test/pipelines';
import { renderWithClient } from '@/test/utils';
import { PipelineEditor } from './PipelineEditor';
import type { PipelineCanvasProps } from './PipelineCanvas';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
// The real canvas is covered in PipelineCanvas.test.tsx; here it is a probe over the editor's props.
vi.mock('./LazyPipelineCanvas', () => ({
  LazyPipelineCanvas: (p: PipelineCanvasProps) => (
    <div
      data-testid="canvas"
      data-invalid-nodes={[...p.invalidNodeIds].sort().join(',')}
      data-invalid-edges={[...p.invalidEdgeKeys].sort().join(',')}
      data-statuses={JSON.stringify([...(p.statuses ?? [])])}
    >
      <button type="button" onClick={() => p.onMoveNode('transform_1', { x: 10.4, y: 20.6 })}>
        drag transform_1
      </button>
      <button type="button" onClick={() => p.onConnect('transform_1', 'db_query_1')}>
        connect transform_1 to db_query_1
      </button>
    </div>
  ),
}));
afterEach(() => vi.unstubAllGlobals());

const base = (): PipelineSpec => ({
  ...emptyPipelineSpec(),
  nodes: [
    { id: 'db_query_1', type: 'db_query', config: {}, inputs: {} },
    { id: 'transform_1', type: 'transform', config: {}, inputs: {} },
    { id: 'sink_1', type: 'sink', config: {}, inputs: {} },
  ],
  edges: [{ from: 'db_query_1', to: 'transform_1' }],
});

function Harness({
  initial,
  validation,
}: {
  initial?: PipelineSpec;
  validation?: ValidationResult;
}) {
  const [spec, setSpec] = useState(initial ?? emptyPipelineSpec());
  return (
    <>
      <PipelineEditor
        org="acme"
        artifact={artifact({ id: 'p-1' })}
        spec={spec}
        onChange={setSpec}
        readOnly={false}
        reportInvalid={() => undefined}
        validation={validation}
      />
      <pre data-testid="spec">{JSON.stringify(spec)}</pre>
      <ul aria-label="blockers">
        {pipelineBlockers(spec).map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </>
  );
}

const specOf = () => JSON.parse(screen.getByTestId('spec').textContent ?? '{}') as PipelineSpec;

async function setup(initial?: PipelineSpec, validation?: ValidationResult) {
  stubPipelines();
  renderWithClient(<Harness initial={initial} validation={validation} />);
  const user = userEvent.setup();
  await screen.findByRole('list', { name: 'Node palette' });
  return user;
}

async function openList(user: ReturnType<typeof userEvent.setup>, node: string, type: string) {
  await user.click(screen.getByRole('button', { name: 'List view' }));
  await user.click(screen.getByRole('button', { name: `Select ${type} node ${node}` }));
}

describe('node palette', () => {
  it('offers only enabled node types and lists the rest greyed out with the reason', async () => {
    await setup();
    const palette = screen.getByRole('list', { name: 'Node palette' });
    expect(within(palette).getByRole('button', { name: 'Add Database query node' })).toBeVisible();
    expect(within(palette).getByRole('button', { name: 'Add Sink node' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Add API request node' })).not.toBeInTheDocument();
    const off = screen.getByRole('list', { name: 'Unavailable node types' });
    expect(within(off).getByText('API request')).toBeInTheDocument();
    expect(within(off).getByText('Executor conformance test has not passed.')).toBeInTheDocument();
    expect(within(off).queryByRole('button')).not.toBeInTheDocument();
  });

  it('makes enabled items draggable and adds a node with a unique id on click', async () => {
    const user = await setup();
    const add = screen.getByRole('button', { name: 'Add Transform node' });
    expect(add).toHaveAttribute('draggable', 'true');
    await user.click(add);
    await user.click(add);
    expect(specOf().nodes.map((n) => n.id)).toEqual(['transform_1', 'transform_2']);
  });
});

describe('input binding', () => {
  it('writes inputs[name] = "node.output" for an upstream output only', async () => {
    const user = await setup(base());
    await openList(user, 'transform_1', 'transform');
    const select = screen.getByLabelText('Input rows');
    const options = within(select)
      .getAllByRole('option')
      .map((o) => o.textContent);
    expect(options).toEqual(['Not bound', 'db_query_1.rows']);
    await user.selectOptions(select, 'db_query_1.rows');
    expect(specOf().nodes.find((n) => n.id === 'transform_1')?.inputs).toEqual({
      rows: 'db_query_1.rows',
    });
    await user.selectOptions(select, '');
    expect(specOf().nodes.find((n) => n.id === 'transform_1')?.inputs).toEqual({});
  });

  it('flags a binding that is not upstream', async () => {
    const s = base();
    s.nodes[2] = { id: 'sink_1', type: 'sink', config: {}, inputs: { doc: 'transform_1.rows' } };
    await setup(s);
    expect(screen.getByRole('list', { name: 'blockers' })).toHaveTextContent(
      'input "doc" is bound to "transform_1.rows", which is not an upstream output',
    );
  });
});

describe('edges', () => {
  it('prevents cycles from the edge editor and keeps the spec unchanged', async () => {
    const user = await setup(base());
    await user.selectOptions(screen.getByLabelText('Edge from'), 'transform_1');
    await user.selectOptions(screen.getByLabelText('Edge to'), 'db_query_1');
    await user.click(screen.getByRole('button', { name: 'Add edge' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('would create a cycle');
    expect(specOf().edges).toEqual([{ from: 'db_query_1', to: 'transform_1' }]);
  });

  it('prevents cycles created on the canvas', async () => {
    const user = await setup(base());
    await user.click(screen.getByRole('button', { name: 'connect transform_1 to db_query_1' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('would create a cycle');
    expect(specOf().edges).toHaveLength(1);
  });

  it('lets condition edges branch on true and false', async () => {
    const s = emptyPipelineSpec();
    s.nodes = [
      { id: 'condition_1', type: 'condition', config: {}, inputs: {} },
      { id: 'sink_1', type: 'sink', config: {}, inputs: {} },
    ];
    s.edges = [{ from: 'condition_1', to: 'sink_1' }];
    const user = await setup(s);
    await user.selectOptions(screen.getByLabelText('When for edge condition_1 to sink_1'), 'false');
    expect(specOf().edges[0]).toEqual({ from: 'condition_1', to: 'sink_1', when: 'false' });
    await user.selectOptions(screen.getByLabelText('When for edge condition_1 to sink_1'), 'true');
    expect(specOf().edges[0]?.when).toBe('true');
    await user.selectOptions(
      screen.getByLabelText('When for edge condition_1 to sink_1'),
      'always',
    );
    expect(specOf().edges[0]).toEqual({ from: 'condition_1', to: 'sink_1' });
  });
});

describe('layout persistence', () => {
  it('stores dragged node positions in spec.ui.positions (rounded)', async () => {
    const user = await setup(base());
    await user.click(screen.getByRole('button', { name: 'drag transform_1' }));
    expect(specOf().ui).toEqual({ positions: { transform_1: { x: 10, y: 21 } } });
  });
});

describe('validation highlighting', () => {
  it('marks nodes and edges named by the server errors on the canvas', async () => {
    await setup(base(), {
      valid: false,
      errors: [
        { path: 'nodes[1]', code: 'schema_incompatible', message: 'bad', node_id: 'transform_1' },
        {
          path: 'edges[0]',
          code: 'dangling_edge',
          message: 'bad edge',
          edge: { from: 'db_query_1', to: 'transform_1' },
        },
      ],
    });
    const canvas = screen.getByTestId('canvas');
    expect(canvas).toHaveAttribute('data-invalid-nodes', 'transform_1');
    expect(canvas).toHaveAttribute('data-invalid-edges', 'db_query_1->transform_1');
  });

  it('highlights client-detected problems and lists advisory warnings', async () => {
    const s = emptyPipelineSpec();
    s.nodes = [
      { id: 'agent_1', type: 'agent', config: {}, inputs: {} },
      { id: 'sink_1', type: 'sink', config: { target: 'connection' }, inputs: {} },
      { id: 'a', type: 'transform', config: {}, inputs: { rows: 'nope.rows' } },
    ];
    s.edges = [{ from: 'agent_1', to: 'sink_1' }];
    await setup(s);
    expect(screen.getByTestId('canvas')).toHaveAttribute('data-invalid-nodes', 'a');
    const checks = screen.getByRole('region', { name: 'Advisory checks' });
    expect(checks).toHaveTextContent('approval_required_before_side_effect');
  });
});

describe('node configuration', () => {
  it('generates a form from config_schema and writes typed config', async () => {
    const user = await setup(base());
    await openList(user, 'db_query_1', 'db_query');
    await user.type(screen.getByLabelText('SQL (required)'), 'select 1');
    await user.type(screen.getByLabelText('Max rows'), '25');
    expect(specOf().nodes[0]?.config).toEqual({ sql: 'select 1', max_rows: 25 });
    expect(screen.getByLabelText('Connection')).toBeInTheDocument();
  });

  it('never writes connection-only keys for a run_artifact sink (server rejects them)', async () => {
    const user = await setup(base());
    await openList(user, 'sink_1', 'sink');
    expect(screen.queryByLabelText('Method')).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Target'), 'run_artifact');
    expect(specOf().nodes[2]?.config).toEqual({ target: 'run_artifact', mode: 'replace' });
    await user.selectOptions(screen.getByLabelText('Target'), 'connection');
    expect(screen.getByLabelText('Method')).toBeInTheDocument();
    expect(specOf().nodes[2]?.config).toMatchObject({ target: 'connection' });
    await user.selectOptions(screen.getByLabelText('Target'), 'run_artifact');
    expect(specOf().nodes[2]?.config).toEqual({ target: 'run_artifact', mode: 'replace' });
  });

  it('shows inline errors for out-of-range values', async () => {
    const user = await setup(base());
    await openList(user, 'db_query_1', 'db_query');
    await user.type(screen.getByLabelText('Max rows'), '0');
    expect(screen.getByText('Must be at least 1.')).toBeInTheDocument();
  });

  it('edits the transform operator list as structured rows', async () => {
    const user = await setup(base());
    await openList(user, 'transform_1', 'transform');
    await user.click(screen.getByRole('button', { name: 'Add filter operator' }));
    await user.type(screen.getByLabelText('Operator 1: filter expression'), "status == 'open'");
    await user.click(screen.getByRole('button', { name: 'Add select operator' }));
    await user.type(screen.getByLabelText('Operator 2: fields to keep'), 'id, status');
    await user.click(screen.getByRole('button', { name: 'Add rename operator' }));
    await user.type(screen.getByLabelText('Operator 3: rename from'), 'status');
    await user.type(screen.getByLabelText('Operator 3: rename to'), 'state');
    await user.click(screen.getByRole('button', { name: 'Add cast operator' }));
    await user.type(screen.getByLabelText('Operator 4: cast field'), 'id');
    await user.selectOptions(screen.getByLabelText('Operator 4: cast to'), 'integer');
    expect(specOf().nodes[1]?.config.operations).toEqual([
      { op: 'filter', expression: "status == 'open'" },
      { op: 'select', fields: ['id', 'status'] },
      { op: 'rename', from: 'status', to: 'state' },
      { op: 'cast', field: 'id', to: 'integer' },
    ]);
  });

  it('reports an invalid filter expression and can reorder and remove rows', async () => {
    const user = await setup(base());
    await openList(user, 'transform_1', 'transform');
    await user.click(screen.getByRole('button', { name: 'Add filter operator' }));
    await user.type(screen.getByLabelText('Operator 1: filter expression'), 'len(x) > 1');
    expect(screen.getByText('Function calls are not allowed.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Add aggregate operator' }));
    await user.click(screen.getByRole('button', { name: 'Move operator 2 up' }));
    const ops = specOf().nodes[1]?.config.operations as { op: string }[];
    expect(ops.map((o) => o.op)).toEqual(['aggregate', 'filter']);
    await user.click(screen.getByRole('button', { name: 'Remove operator 1' }));
    expect((specOf().nodes[1]?.config.operations as unknown[]).length).toBe(1);
  });

  it('edits params schema and policies', async () => {
    const user = await setup(base());
    await user.selectOptions(screen.getByLabelText('On failure'), 'continue');
    expect(specOf().policies.on_failure).toBe('continue');
    const box = screen.getByLabelText('Parameters schema (JSON)');
    await user.clear(box);
    await user.type(box, '{{"type": "object", "properties": {{"region": {{"type": "string"}}}}');
    expect(specOf().params).toEqual({ type: 'object', properties: { region: { type: 'string' } } });
  });
});

describe('server-shaped condition edges (amendment E1)', () => {
  const branchSpec = (when: unknown): PipelineSpec => {
    const s = emptyPipelineSpec();
    s.nodes = [
      { id: 'risk', type: 'condition', config: {}, inputs: {} },
      { id: 'gate', type: 'approval', config: {}, inputs: {} },
    ];
    s.edges = [{ from: 'risk', to: 'gate', ...(when === undefined ? {} : { when }) }];
    return s;
  };

  it('shows "true" from the server string as a normal branch, not a custom condition', async () => {
    await setup(branchSpec('true'));
    expect(screen.queryByText(/Custom condition/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('When for edge risk to gate')).toHaveValue('true');
  });

  it('treats the served "always" as the default option', async () => {
    await setup(branchSpec('always'));
    expect(screen.getByLabelText('When for edge risk to gate')).toHaveValue('always');
  });

  it('reads legacy boolean conditions as branches', async () => {
    await setup(branchSpec(false));
    expect(screen.getByLabelText('When for edge risk to gate')).toHaveValue('false');
  });

  it('still shows unsupported condition objects as custom', async () => {
    await setup(branchSpec({ '==': [1, 1] }));
    expect(screen.getByText(/Custom condition/)).toBeInTheDocument();
  });
});

describe('dynamic input ports', () => {
  const condSpec = (): PipelineSpec => {
    const s = emptyPipelineSpec();
    s.nodes = [
      { id: 'rollup', type: 'transform', config: {}, inputs: {} },
      { id: 'risk', type: 'condition', config: {}, inputs: {} },
    ];
    s.edges = [{ from: 'rollup', to: 'risk' }];
    return s;
  };

  it('lets a condition node name and bind its own inputs, and remove them again', async () => {
    const user = await setup(condSpec());
    await openList(user, 'risk', 'condition');
    expect(screen.queryByLabelText('Input value')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('New input name'), 'rollup');
    await user.click(screen.getByRole('button', { name: 'Add input' }));
    expect(specOf().nodes[1]?.inputs).toEqual({ rollup: 'rollup.rows' });
    expect(screen.getByLabelText('Input rollup')).toHaveValue('rollup.rows');
    await user.click(screen.getByRole('button', { name: 'Remove input rollup' }));
    expect(specOf().nodes[1]?.inputs).toEqual({});
  });

  it('rejects names the server cannot use as expression ports', async () => {
    const user = await setup(condSpec());
    await openList(user, 'risk', 'condition');
    await user.type(screen.getByLabelText('New input name'), '1bad');
    expect(screen.getByRole('button', { name: 'Add input' })).toBeDisabled();
  });

  it('does not report a registry-declared dynamic port as unknown', async () => {
    const s = condSpec();
    s.nodes[1] = {
      id: 'risk',
      type: 'condition',
      config: { expression: 'inputs.rollup[0].max_open > params.t' },
      inputs: { rollup: 'rollup.rows' },
    };
    await setup(s);
    expect(screen.queryByText(/has no input port/)).not.toBeInTheDocument();
    expect(screen.getByTestId('canvas')).toHaveAttribute('data-invalid-nodes', '');
  });
});
