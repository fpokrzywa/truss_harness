import { useState } from 'react';
import type { AppApi } from './api';
import { ChatScreen } from './ChatScreen';
import type { Screen } from './screens';
import { TableScreen } from './TableScreen';

/** Draws a generated app's screens with one navigation entry per screen; opens the first.
 * `embedded` (a page that has its own `main`, as SETH's preview) wraps the screen in a `div`. */
export function ScreenRenderer({
  screens,
  api,
  embedded = false,
}: {
  screens: Screen[];
  api: AppApi;
  embedded?: boolean;
}) {
  const [active, setActive] = useState(0);
  const screen = screens[active];
  const Content = embedded ? 'div' : 'main';

  return (
    <div className="app">
      <nav aria-label="Screens">
        {screens.map((item, index) => (
          <button
            key={index}
            type="button"
            aria-current={index === active ? 'page' : undefined}
            onClick={() => setActive(index)}
          >
            {item.title}
          </button>
        ))}
      </nav>
      <Content>
        {screen?.kind === 'chat' && <ChatScreen key={active} screen={screen} api={api} />}
        {screen?.kind === 'table' && <TableScreen key={active} screen={screen} api={api} />}
      </Content>
    </div>
  );
}
