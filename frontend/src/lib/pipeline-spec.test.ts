import { describe, expect, it } from 'vitest';
import type { PipelineNode, PipelineSpec } from './api/pipeline-types';
import { NODE_TYPES } from '@/test/pipelines';
import {
  addEdgeChecked,
  ancestorsOf,
  bindInput,
  bindingOptions,
  edgeLabel,
  edgeWhen,
  emptyPipelineSpec,
  layoutPositions,
  serializeOperators,
  withEdgeWhen,
  newOperator,
  normalizePipelineSpec,
  operatorProblem,
  parseOperators,
  pipelineBlockers,
  pipelineIssues,
  removePipelineNode,
  setPosition,
} from './pipeline-spec';

const node = (id: string, type: string, over: Partial<PipelineNode> = {}): PipelineNode => ({
  id,
  type,
  config: {},
  inputs: {},
  ...over,
});

const spec = (nodes: PipelineNode[], edges: [string, string][] = []): PipelineSpec => ({
  ...emptyPipelineSpec(),
  nodes,
  edges: edges.map(([from, to]) => ({ from, to })),
});

describe('normalizePipelineSpec', () => {
  it('fills defaults and keeps unknown keys', () => {
    const s = normalizePipelineSpec({
      nodes: [{ id: 'a', type: 'db_query' }],
      extra_key: 1,
      policies: { max_parallel: 2 },
    });
    expect(s.schema_version).toBe('1');
    expect(s.policies).toEqual({
      max_parallel: 2,
      deadline_seconds: 3600,
      on_failure: 'fail_fast',
    });
    expect(s.nodes[0]).toMatchObject({ config: {}, inputs: {} });
    expect(s.extra_key).toBe(1);
  });
});

describe('edges and cycles', () => {
  const s = spec(
    [node('a', 'db_query'), node('b', 'transform'), node('c', 'sink')],
    [
      ['a', 'b'],
      ['b', 'c'],
    ],
  );

  it('rejects an edge that would close a cycle, including through a chain', () => {
    const r = addEdgeChecked(s, 'c', 'a');
    expect(r.error).toMatch(/cycle/);
    expect(r.spec.edges).toHaveLength(2);
  });

  it('rejects self edges and edges to missing nodes', () => {
    expect(addEdgeChecked(s, 'a', 'a').error).toMatch(/cycle/);
    expect(addEdgeChecked(s, 'a', 'zzz').error).toMatch(/missing node/);
  });

  it('adds valid edges once and keeps an optional when', () => {
    const r = addEdgeChecked(s, 'a', 'c', 'true');
    expect(r.error).toBeUndefined();
    expect(r.spec.edges.at(-1)).toEqual({ from: 'a', to: 'c', when: 'true' });
    expect(addEdgeChecked(r.spec, 'a', 'c').spec.edges).toHaveLength(3);
  });

  it('computes ancestors transitively', () => {
    expect([...ancestorsOf('c', s.edges)].sort()).toEqual(['a', 'b']);
  });
});

describe('bindings', () => {
  const s = spec(
    [node('src', 'db_query'), node('xf', 'transform'), node('other', 'db_query')],
    [['src', 'xf']],
  );
  const withParams = {
    ...s,
    params: { type: 'object', properties: { region: { type: 'string' } } },
  };

  it('offers upstream outputs and params only', () => {
    expect(bindingOptions(withParams, NODE_TYPES, 'xf').map((o) => o.value)).toEqual([
      'src.rows',
      '$params.region',
    ]);
  });

  it('writes and clears inputs[name] = "node.output"', () => {
    const bound = bindInput(node('xf', 'transform'), 'rows', 'src.rows');
    expect(bound.inputs).toEqual({ rows: 'src.rows' });
    expect(bindInput(bound, 'rows', '').inputs).toEqual({});
  });

  it('drops bindings that point at a removed node', () => {
    const t = {
      ...s,
      nodes: s.nodes.map((n) => (n.id === 'xf' ? bindInput(n, 'rows', 'src.rows') : n)),
    };
    const r = removePipelineNode(t, 'src');
    expect(r.nodes.find((n) => n.id === 'xf')?.inputs).toEqual({});
    expect(r.edges).toEqual([]);
  });
});

describe('positions', () => {
  it('persists rounded positions under spec.ui.positions and prefers them over auto layout', () => {
    const s = setPosition(spec([node('a', 'db_query')]), 'a', { x: 10.4, y: 20.6 });
    expect(s.ui).toEqual({ positions: { a: { x: 10, y: 21 } } });
    expect(layoutPositions(s).get('a')).toEqual({ x: 10, y: 21 });
  });

  it('auto-lays out nodes without a stored position', () => {
    const s = spec([node('a', 'db_query'), node('b', 'transform')], [['a', 'b']]);
    expect(layoutPositions(s).get('b')?.x).toBeGreaterThan(layoutPositions(s).get('a')?.x ?? 0);
  });
});

describe('pipelineIssues', () => {
  const codes = (s: PipelineSpec, reg = NODE_TYPES) => pipelineIssues(s, reg).map((i) => i.code);

  it('flags duplicate ids, dangling edges and cycles as blockers', () => {
    const s = spec(
      [node('a', 'transform'), node('a', 'transform'), node('b', 'transform')],
      [
        ['a', 'ghost'],
        ['a', 'b'],
        ['b', 'a'],
      ],
    );
    expect(codes(s)).toEqual(
      expect.arrayContaining(['duplicate_node_id', 'dangling_edge', 'cycle_detected']),
    );
    expect(pipelineBlockers(s).length).toBeGreaterThanOrEqual(3);
  });

  it('flags an input bound to a node that is not upstream', () => {
    const s = spec([node('a', 'db_query'), node('b', 'transform', { inputs: { rows: 'a.rows' } })]);
    const issue = pipelineIssues(s, NODE_TYPES).find((i) => i.code === 'input_not_upstream');
    expect(issue?.nodeId).toBe('b');
    expect(pipelineIssues({ ...s, edges: [{ from: 'a', to: 'b' }] }, NODE_TYPES)).toEqual([]);
  });

  it('accepts $params bindings', () => {
    const s = spec([node('b', 'transform', { inputs: { rows: '$params.rows' } })]);
    expect(codes(s)).not.toContain('input_not_upstream');
  });

  it('checks condition and transform expressions with the safe expression parser', () => {
    const s = spec([
      node('c', 'condition', { config: { expression: 'len(a.b) > 1' } }),
      node('t', 'transform', {
        config: { operations: [{ op: 'filter', expression: 'status ==' }] },
      }),
    ]);
    const bad = pipelineIssues(s, NODE_TYPES).filter((i) => i.code === 'expression_invalid');
    expect(bad.map((i) => i.nodeId).sort()).toEqual(['c', 't']);
    const ok = spec([
      node('c', 'condition', { config: { expression: 'params.a > 1 and not params.b' } }),
    ]);
    expect(codes(ok)).not.toContain('expression_invalid');
  });

  it('reports unknown and disabled node types without blocking save', () => {
    const s = spec([node('x', 'mystery'), node('r', 'api_request')]);
    const issues = pipelineIssues(s, NODE_TYPES);
    expect(issues.map((i) => i.code)).toEqual(['unknown_node_type', 'node_type_disabled']);
    expect(issues.every((i) => !i.blocking)).toBe(true);
    expect(issues[1]?.message).toContain('Executor conformance test has not passed.');
  });

  describe('side effect after model output', () => {
    const chain = (...ids: [string, string][]) => ids;
    const nodes = (withApproval: boolean) => [
      node('ai', 'agent'),
      ...(withApproval ? [node('gate', 'approval')] : []),
      node('out', 'sink', { config: { target: 'connection' } }),
    ];

    it('warns when a connection sink follows an agent without an approval on the path', () => {
      const w = pipelineIssues(spec(nodes(false), chain(['ai', 'out'])), NODE_TYPES).filter(
        (i) => i.code === 'approval_required_before_side_effect',
      );
      expect(w).toHaveLength(1);
      expect(w[0]).toMatchObject({ nodeId: 'out', severity: 'warning', blocking: false });
    });

    it('does not warn when an approval node is on the path', () => {
      const s = spec(nodes(true), chain(['ai', 'gate'], ['gate', 'out']));
      expect(codes(s)).not.toContain('approval_required_before_side_effect');
    });

    it('does not warn for a run_artifact sink', () => {
      const s = spec(
        [node('ai', 'agent'), node('out', 'sink', { config: { target: 'run_artifact' } })],
        [['ai', 'out']],
      );
      expect(codes(s)).not.toContain('approval_required_before_side_effect');
    });

    it('treats a non-GET api_request as a side effect', () => {
      const s = spec(
        [node('ai', 'agent'), node('call', 'api_request', { config: { method: 'POST' } })],
        [['ai', 'call']],
      );
      expect(codes(s)).toContain('approval_required_before_side_effect');
    });
  });
});

describe('transform operators', () => {
  it('creates a row per operator kind and describes what is missing', () => {
    expect(operatorProblem(newOperator('select'))).toMatch(/at least one field/);
    expect(operatorProblem({ op: 'select', fields: ['a'] })).toBeNull();
    expect(operatorProblem(newOperator('rename'))).toMatch(/source and a target/);
    expect(operatorProblem({ op: 'filter', expression: 'status ==' })).not.toBeNull();
    expect(operatorProblem({ op: 'filter', expression: "row.status == 'open'" })).toBeNull();
    expect(
      operatorProblem({ op: 'aggregate', group_by: [], fn: 'sum', field: '', as: 'total' }),
    ).toMatch(/field/);
    expect(
      operatorProblem({ op: 'aggregate', group_by: [], fn: 'count', field: '', as: 'n' }),
    ).toBeNull();
  });

  it('parses stored operators and drops unknown ones', () => {
    expect(parseOperators([{ op: 'rename', from: 'a', to: 'b' }, { op: 'eval' }, 'x'])).toEqual([
      { op: 'rename', from: 'a', to: 'b' },
    ]);
    expect(parseOperators(undefined)).toEqual([]);
  });
});

describe('edge conditions (amendment E1 strings)', () => {
  it('reads the server strings, tolerates legacy booleans and treats missing / null as always', () => {
    expect(edgeWhen({ when: 'true' })).toBe('true');
    expect(edgeWhen({ when: 'false' })).toBe('false');
    expect(edgeWhen({ when: 'always' })).toBe('always');
    expect(edgeWhen({})).toBe('always');
    expect(edgeWhen({ when: null })).toBe('always');
    expect(edgeWhen({ when: true })).toBe('true');
    expect(edgeWhen({ when: false })).toBe('false');
    expect(edgeWhen({ when: { '==': [1, 1] } })).toBe('custom');
  });

  it('labels only true / false branches (always has no label)', () => {
    expect(edgeLabel({ when: 'always' })).toBeUndefined();
    expect(edgeLabel({})).toBeUndefined();
    expect(edgeLabel({ when: 'true' })).toBe('true');
    expect(edgeLabel({ when: 'false' })).toBe('false');
    expect(edgeLabel({ when: { '==': [1, 1] } })).toBe('condition');
  });

  it('writes strings, and omits the key for always', () => {
    expect(withEdgeWhen({ from: 'a', to: 'b' }, 'true')).toEqual({
      from: 'a',
      to: 'b',
      when: 'true',
    });
    expect(withEdgeWhen({ from: 'a', to: 'b', when: 'false' }, 'always')).toEqual({
      from: 'a',
      to: 'b',
    });
  });

  it('rewrites legacy boolean / null conditions on load and keeps "always" as served', () => {
    const n = normalizePipelineSpec({
      edges: [
        { from: 'a', to: 'b', when: true },
        { from: 'a', to: 'c', when: false },
        { from: 'a', to: 'd', when: null },
        { from: 'a', to: 'e', when: 'always' },
      ],
    });
    expect(n.edges).toEqual([
      { from: 'a', to: 'b', when: 'true' },
      { from: 'a', to: 'c', when: 'false' },
      { from: 'a', to: 'd' },
      { from: 'a', to: 'e', when: 'always' },
    ]);
  });

  it('flags non-E1 conditions and true / false on non-condition sources', () => {
    const s = {
      ...spec([node('c', 'condition'), node('t', 'transform'), node('x', 'sink')]),
      edges: [
        { from: 'c', to: 't', when: 'true' },
        { from: 'c', to: 'x', when: { '==': [1, 1] } },
        { from: 't', to: 'x', when: 'false' },
      ],
    };
    const found = pipelineIssues(s).filter((i) => i.edge);
    expect(found.map((i) => `${i.code}:${i.edge?.from}->${i.edge?.to}`)).toEqual([
      'expression_invalid:c->x',
      'invalid_edge_condition:t->x',
    ]);
  });
});

describe('condition expressions and ports (parity with the server)', () => {
  const codes = (s: PipelineSpec) => pipelineIssues(s, NODE_TYPES).map((i) => i.code);
  const rollup = (config: Record<string, unknown> = {}) =>
    node('risk', 'condition', {
      config: { expression: 'inputs.rollup[0].max_open > params.threshold', ...config },
      inputs: { rollup: 'rollup.data' },
    });
  const withUpstream = (n: PipelineNode) => ({
    ...spec([node('rollup', 'transform'), n]),
    edges: [{ from: 'rollup', to: 'risk' }],
  });

  it('accepts the reference pipeline condition (indexing, inputs and params roots)', () => {
    const s = withUpstream(rollup());
    expect(pipelineIssues(s, NODE_TYPES).filter((i) => i.nodeId === 'risk')).toEqual([]);
    expect(pipelineBlockers(s)).toEqual([]);
  });

  it('rejects roots other than inputs / params and unbound input ports, like the server', () => {
    expect(codes(withUpstream(rollup({ expression: 'nodes.rollup.x > 1' })))).toContain(
      'expression_invalid',
    );
    const unbound = pipelineIssues(withUpstream(rollup({ expression: 'inputs.nope > 1' })));
    expect(unbound.find((i) => i.code === 'expression_invalid')?.message).toMatch(
      /bound input port/,
    );
  });

  it('accepts row / params in transform expressions and rejects other roots', () => {
    const ok = spec([
      node('t', 'transform', {
        config: {
          operations: [{ op: 'filter', expression: "row.status == 'open' and row.n > params.min" }],
        },
      }),
    ]);
    expect(codes(ok)).not.toContain('expression_invalid');
    const bad = spec([
      node('t', 'transform', {
        config: { operations: [{ op: 'map', field: 'x', expression: 'inputs.a' }] },
      }),
    ]);
    expect(codes(bad)).toContain('expression_invalid');
  });

  it('honours dynamic_inputs: undeclared ports are unknown unless the type names its own inputs', () => {
    const ports = (type: string) =>
      pipelineIssues(
        {
          ...spec([node('a', 'db_query'), node('n', type, { inputs: { made_up: 'a.rows' } })]),
          edges: [{ from: 'a', to: 'n' }],
        },
        NODE_TYPES,
      ).filter((i) => i.code === 'unknown_port');
    expect(ports('transform')).toHaveLength(1);
    expect(ports('transform')[0]?.blocking).toBe(false);
    expect(ports('condition')).toEqual([]);
  });
});

describe('transform operations as stored', () => {
  it('omits an empty aggregate field (the server counts non-null values of a named field)', () => {
    expect(
      serializeOperators([{ op: 'aggregate', group_by: ['c'], fn: 'count', field: '', as: 'n' }]),
    ).toEqual([{ op: 'aggregate', group_by: ['c'], fn: 'count', as: 'n' }]);
    expect(
      serializeOperators([{ op: 'aggregate', group_by: [], fn: 'sum', field: 'v', as: 's' }]),
    ).toEqual([{ op: 'aggregate', group_by: [], fn: 'sum', field: 'v', as: 's' }]);
  });

  it('flags field names the server rejects', () => {
    expect(operatorProblem({ op: 'select', fields: ['order id'] })).toMatch(/Field names/);
    expect(operatorProblem({ op: 'rename', from: 'a', to: '1x' })).toMatch(/Field names/);
  });
});
