import { useEffect, useState } from 'react';
import { httpApi } from './api';
import { ScreenRenderer } from './ScreenRenderer';
import { parseScreens, type Screen } from './screens';

type State = { status: 'loading' } | { status: 'error' } | { status: 'ready'; screens: Screen[] };

async function loadScreens(): Promise<Screen[] | null> {
  try {
    const response = await fetch('config/screens.json');
    if (!response.ok) return null;
    return parseScreens(await response.json());
  } catch {
    return null;
  }
}

/** The bundle's root: reads config/screens.json next to the page, then draws its screens. */
export function App() {
  const [state, setState] = useState<State>({ status: 'loading' });

  useEffect(() => {
    let cancelled = false;
    void loadScreens().then((screens) => {
      if (cancelled) return;
      setState(screens ? { status: 'ready', screens } : { status: 'error' });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.status === 'loading') return <p className="loading">Loading...</p>;
  if (state.status === 'error') {
    return (
      <p role="alert" className="error">
        The app configuration could not be loaded.
      </p>
    );
  }
  return <ScreenRenderer screens={state.screens} api={httpApi} />;
}
