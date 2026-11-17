import { describe, expect, it } from 'vitest';
import type { OrchNode, OrchestrationSpec } from './api/operate-types';
import {
  autoLayout,
  emptyOrchestrationSpec,
  findCycle,
  graphIssues,
  nextNodeId,
  removeNode,
} from './graph';

const spec = (over: Partial<OrchestrationSpec>): OrchestrationSpec => ({
  ...emptyOrchestrationSpec(),
  ...over,
});
const n = (id: string, type: OrchNode['type'] = 'approval'): OrchNode => ({
  id,
  type,
  input_map: {},
});

describe('cycle detection', () => {
  it('returns null for a DAG including diamonds', () => {
    const nodes = ['a', 'b', 'c', 'd'].map((id) => ({ id }));
    const edges = [
      { from: 'a', to: 'b' },
      { from: 'a', to: 'c' },
      { from: 'b', to: 'd' },
      { from: 'c', to: 'd' },
    ];
    expect(findCycle(nodes, edges)).toBeNull();
  });

  it('finds a cycle and reports its path', () => {
    const nodes = ['a', 'b', 'c'].map((id) => ({ id }));
    const edges = [
      { from: 'a', to: 'b' },
      { from: 'b', to: 'c' },
      { from: 'c', to: 'b' },
    ];
    expect(findCycle(nodes, edges)).toEqual(['b', 'c', 'b']);
  });

  it('treats a self loop as a cycle', () => {
    expect(findCycle([{ id: 'a' }], [{ from: 'a', to: 'a' }])).toEqual(['a', 'a']);
  });
});

describe('graph issues (client-side mirror of server validation)', () => {
  it('flags cycles as blockers', () => {
    const issues = graphIssues(
      spec({
        nodes: [n('a'), n('b')],
        edges: [
          { from: 'a', to: 'b' },
          { from: 'b', to: 'a' },
        ],
      }),
    );
    expect(issues.map((i) => i.message).join('\n')).toMatch(/Cycle detected: a -> b -> a/);
  });

  it('flags call syntax in a condition and a missing agent ref', () => {
    const issues = graphIssues(
      spec({
        nodes: [{ ...n('c1', 'condition'), condition: 'len(x.y) > 1' }, n('a1', 'agent')],
      }),
    );
    const text = issues.map((i) => i.message).join('\n');
    expect(text).toMatch(/c1" condition: Function calls are not allowed/);
    expect(text).toMatch(/a1": choose a published agent version/);
  });

  it('is clean for a valid graph and rejects true/false edges from non-conditions', () => {
    const good = spec({
      nodes: [{ ...n('c1', 'condition'), condition: 'a.b == 1' }, n('ap')],
      edges: [{ from: 'c1', to: 'ap', when: 'true' }],
    });
    expect(graphIssues(good)).toEqual([]);
    const bad = spec({
      nodes: [n('ap'), n('ap2')],
      edges: [{ from: 'ap', to: 'ap2', when: 'true' }],
    });
    expect(graphIssues(bad).map((i) => i.message)).toEqual([
      expect.stringMatching(/only condition nodes can branch/),
    ]);
  });

  it('requires at least one node and rejects duplicate ids', () => {
    expect(graphIssues(spec({})).map((i) => i.message)).toContain('Add at least one node.');
    expect(graphIssues(spec({ nodes: [n('x'), n('x')] })).map((i) => i.message)).toContain(
      'Duplicate node id "x".',
    );
  });
});

describe('graph helpers', () => {
  it('generates unique node ids and removes a node with its edges and branch refs', () => {
    expect(nextNodeId([n('agent_1')], 'agent')).toBe('agent_2');
    const s = spec({
      nodes: [{ ...n('p', 'parallel'), branches: ['a', 'b'] }, n('a'), n('b')],
      edges: [
        { from: 'p', to: 'a' },
        { from: 'a', to: 'b' },
      ],
    });
    const out = removeNode(s, 'a');
    expect(out.nodes.map((x) => x.id)).toEqual(['p', 'b']);
    expect(out.nodes[0]?.branches).toEqual(['b']);
    expect(out.edges).toEqual([]);
  });

  it('lays out layers left to right and terminates on cycles', () => {
    const pos = autoLayout(
      [{ id: 'a' }, { id: 'b' }],
      [
        { from: 'a', to: 'b' },
        { from: 'b', to: 'a' },
      ],
    );
    expect(pos.get('a')?.x).toBe(0);
    expect(pos.get('b')?.x).toBe(240);
  });
});
