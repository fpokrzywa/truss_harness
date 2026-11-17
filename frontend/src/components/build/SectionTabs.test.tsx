import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { Section } from './shared';
import { SectionTabs, useSectionFirstShown } from './SectionTabs';

const tabNames = () => screen.getAllByRole('tab').map((t) => t.textContent);

function Editor({ third = false }: { third?: boolean }) {
  return (
    <SectionTabs label="Editor sections">
      <Section title="First">
        <label>
          Name
          <input />
        </label>
      </Section>
      {third && (
        <Section title="Middle">
          <p>Middle content</p>
        </Section>
      )}
      <Section title="Last" help="Last help">
        <p>Last content</p>
        <Section title="Nested">
          <p>Nested content</p>
        </Section>
      </Section>
    </SectionTabs>
  );
}

describe('SectionTabs', () => {
  it('shows one section at a time, starting with the first', async () => {
    render(<Editor />);
    expect(tabNames()).toEqual(['First', 'Last']);
    expect(screen.getByRole('tab', { name: 'First' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByLabelText('Name')).toBeVisible();
    expect(screen.queryByText('Last content')).not.toBeVisible();

    await userEvent.click(screen.getByRole('tab', { name: 'Last' }));
    expect(screen.getByText('Last content')).toBeVisible();
    expect(screen.getByText('Last help')).toBeVisible();
    expect(screen.getByLabelText('Name', { selector: 'input' })).not.toBeVisible();
  });

  it('keeps typed text when moving between tabs', async () => {
    const user = userEvent.setup();
    render(<Editor />);
    await user.type(screen.getByLabelText('Name'), 'half typed');
    await user.click(screen.getByRole('tab', { name: 'Last' }));
    await user.click(screen.getByRole('tab', { name: 'First' }));
    expect(screen.getByLabelText('Name')).toHaveValue('half typed');
  });

  it('leaves a section inside a section as a plain card', async () => {
    render(<Editor />);
    expect(tabNames()).not.toContain('Nested');
    await userEvent.click(screen.getByRole('tab', { name: 'Last' }));
    expect(screen.getByText('Nested content')).toBeVisible();
  });

  it('moves by arrow keys and wraps around', async () => {
    const user = userEvent.setup();
    render(<Editor />);
    await user.click(screen.getByRole('tab', { name: 'First' }));
    await user.keyboard('{ArrowLeft}');
    expect(screen.getByRole('tab', { name: 'Last' })).toHaveFocus();
    expect(screen.getByText('Last content')).toBeVisible();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: 'First' })).toHaveFocus();
  });

  it('puts a section that appears later in page order, and survives one disappearing', async () => {
    const user = userEvent.setup();
    function Toggle() {
      const [third, setThird] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setThird((t) => !t)}>
            toggle
          </button>
          <Editor third={third} />
        </>
      );
    }
    render(<Toggle />);
    await user.click(screen.getByRole('button', { name: 'toggle' }));
    expect(tabNames()).toEqual(['First', 'Middle', 'Last']);

    await user.click(screen.getByRole('tab', { name: 'Middle' }));
    await user.click(screen.getByRole('button', { name: 'toggle' }));
    expect(tabNames()).toEqual(['First', 'Last']);
    expect(screen.getByRole('tab', { name: 'First' })).toHaveAttribute('aria-selected', 'true');
  });

  it('tells content when its section is first shown, and stays true after', async () => {
    const user = userEvent.setup();
    function Measures({ name }: { name: string }) {
      return <p>{`${name}: ${useSectionFirstShown() ? 'drawn' : 'waiting'}`}</p>;
    }
    render(
      <SectionTabs label="Editor sections">
        <Section title="One">
          <Measures name="one" />
        </Section>
        <Section title="Two">
          <Measures name="two" />
        </Section>
      </SectionTabs>,
    );
    expect(screen.getByText('one: drawn')).toBeVisible();
    expect(screen.getByText('two: waiting')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Two' }));
    expect(screen.getByText('two: drawn')).toBeVisible();
    await user.click(screen.getByRole('tab', { name: 'One' }));
    expect(screen.getByText('two: drawn')).toBeInTheDocument();
  });

  it('shows a lone section with its title and no tab row', () => {
    render(
      <SectionTabs label="Editor sections">
        <Section title="Only">
          <p>Only content</p>
        </Section>
      </SectionTabs>,
    );
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.getByRole('group', { name: 'Only' })).toBeVisible();
    expect(screen.getByText('Only content')).toBeVisible();
  });

  it('stacks sections as before when there is no tab host', () => {
    render(
      <>
        <Section title="A">
          <p>A content</p>
        </Section>
        <Section title="B">
          <p>B content</p>
        </Section>
      </>,
    );
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.getByText('A content')).toBeVisible();
    expect(screen.getByText('B content')).toBeVisible();
  });
});
