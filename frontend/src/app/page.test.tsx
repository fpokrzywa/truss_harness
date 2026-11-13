import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import HomePage from './page';

describe('HomePage', () => {
  it('renders the product name and a status line without any backend', () => {
    render(<HomePage />);
    expect(
      screen.getByRole('heading', { name: 'SHI Enterprise Truss Harness' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Front-end shell is running');
  });
});
