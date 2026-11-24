import { useEffect, useEffectEvent } from 'react';

export function useTaskPolling(enabled: boolean, intervalMs: number, refresh: () => void | Promise<void>) {
  const refreshCurrent = useEffectEvent(refresh);

  useEffect(() => {
    if (!enabled) return;

    let refreshing = false;
    async function tick() {
      if (refreshing || document.visibilityState !== 'visible') return;

      refreshing = true;
      try {
        await refreshCurrent();
      } finally {
        refreshing = false;
      }
    }

    const refreshVisible = () => { void tick(); };
    const timer = window.setInterval(refreshVisible, intervalMs);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [enabled, intervalMs]);
}
