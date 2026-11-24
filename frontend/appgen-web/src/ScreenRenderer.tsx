import { useState } from 'react';
import type { AppApi } from './api';
import { ChatScreen } from './ChatScreen';
import type { Screen } from './screens';
import { TableScreen } from './TableScreen';

/** Draws a generated app's screens with one navigation entry per screen; opens the first. */
export function ScreenRenderer({ screens, api }: { screens: Screen[]; api: AppApi }) {
  const [active, setActive] = useState(0);
  const screen = screens[active];

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
      <main>
        {screen?.kind === 'chat' && <ChatScreen key={active} screen={screen} api={api} />}
        {screen?.kind === 'table' && <TableScreen key={active} screen={screen} api={api} />}
      </main>
    </div>
  );
}
