import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { AppApi } from './api';
import { ScreenRenderer } from './ScreenRenderer';

const SCREENS = [{ kind: 'chat' as const, title: 'Ask' }];
const API: AppApi = {
  ask: () => Promise.resolve({ answer: '', tool_calls: [] }),
  runTool: () => Promise.resolve({ columns: [], rows: [], row_count: 0, truncated: false }),
};

describe('ScreenRenderer', () => {
  it('wraps the screen in a main landmark by default', () => {
    render(<ScreenRenderer screens={SCREENS} api={API} />);
    expect(screen.getByRole('main')).toHaveTextContent('Ask');
  });

  it('adds no main landmark when embedded', () => {
    render(<ScreenRenderer screens={SCREENS} api={API} embedded />);
    expect(screen.queryByRole('main')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Ask' })).toBeInTheDocument();
  });
});
