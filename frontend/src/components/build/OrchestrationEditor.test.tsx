import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OrchestrationSpec } from '@/lib/api/operate-types';
import { emptyOrchestrationSpec } from '@/lib/graph';
import { artifact } from '@/test/build';
import { stubOperate } from '@/test/operate';
import { list } from '@/test/security';
import { renderWithClient } from '@/test/utils';
import { OrchestrationEditor, orchestrationBlockers } from './OrchestrationEditor';

vi.mock('next/navigation', () => ({ useParams: () => ({ org: 'acme' }) }));
afterEach(() => vi.unstubAllGlobals());

beforeEach(() => {
  // React Flow measures with ResizeObserver, which jsdom lacks.
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

function Harness({ initial }: { initial?: OrchestrationSpec }) {
  const [spec, setSpec] = useState(initial ?? emptyOrchestrationSpec());
  return (
    <>
      <OrchestrationEditor
        org="acme"
        artifact={artifact({ id: 'o-1' })}
        spec={spec}
        onChange={setSpec}
        readOnly={false}
        reportInvalid={() => undefined}
      />
      <pre data-testid="spec">{JSON.stringify(spec)}</pre>
      <ul aria-label="blockers">
        {orchestrationBlockers(spec).map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </>
  );
}

function setup(initial?: OrchestrationSpec) {
  stubOperate({
    'GET /api/v1/orgs/acme/agents': () => list([]),
    'GET /api/v1/orgs/acme/tools': () => list([]),
  });
  renderWithClient(<Harness initial={initial} />);
  return userEvent.setup();
}

describe('OrchestrationEditor', () => {
  it('rejects call syntax in a condition expression', async () => {
    const user = setup();
    await user.click(screen.getByRole('button', { name: 'List view' }));
    await user.click(screen.getByRole('button', { name: 'Add condition node' }));
    const box = await screen.findByLabelText('Condition expression');
    await user.type(box, 'len(a.b) > 1');
    expect(screen.getByText('Function calls are not allowed.')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'blockers' })).toHaveTextContent('Function calls');
    await user.clear(box);
    await user.type(box, 'a.b > 1');
    expect(screen.queryByText('Function calls are not allowed.')).not.toBeInTheDocument();
    const saved = JSON.parse(screen.getByTestId('spec').textContent ?? '{}') as OrchestrationSpec;
    expect(saved.nodes[0]?.condition).toBe('a.b > 1');
  });

  it('detects cycles created through the edge editor and clears them when removed', async () => {
    const user = setup();
    await user.click(screen.getByRole('button', { name: 'List view' }));
    await user.click(screen.getByRole('button', { name: 'Add approval node' }));
    await user.click(screen.getByRole('button', { name: 'Add parallel node' }));
    const blockers = screen.getByRole('list', { name: 'blockers' });
    const addEdge = async (from: string, to: string) => {
      await user.selectOptions(screen.getByLabelText('Edge from'), from);
      await user.selectOptions(screen.getByLabelText('Edge to'), to);
      await user.click(screen.getByRole('button', { name: 'Add edge' }));
    };
    await addEdge('approval_1', 'parallel_1');
    expect(blockers).not.toHaveTextContent('Cycle detected');
    await addEdge('parallel_1', 'approval_1');
    expect(blockers).toHaveTextContent('Cycle detected: approval_1 -> parallel_1 -> approval_1');
    await user.click(screen.getByRole('button', { name: 'Remove edge parallel_1 to approval_1' }));
    expect(blockers).not.toHaveTextContent('Cycle detected');
  });

  it('offers a keyboard-reachable node list alternative to the canvas', async () => {
    const user = setup({
      ...emptyOrchestrationSpec(),
      nodes: [{ id: 'gate', type: 'approval', input_map: {} }],
    });
    await user.click(screen.getByRole('button', { name: 'List view' }));
    await user.click(screen.getByRole('button', { name: 'Select approval node gate' }));
    expect(screen.getByRole('region', { name: 'Configure node gate' })).toBeInTheDocument();
    render(<p>done</p>);
  });
});
