// Polls a run list: every 5 s while one of its runs is live, every 60 s
// otherwise, and not at all while the tab is hidden. Every open page used
// to ask GET /runs every 5 s whether anything was running or not.
import { useEffect, useRef } from 'react';

export const LIVE_POLL_MS = 5000;
export const IDLE_POLL_MS = 60000;
const LIVE_STATUSES = ['running', 'queued'];

export const hasLiveRun = (runs) =>
  (runs || []).some((r) => LIVE_STATUSES.includes(r && r.status));

// fetchRuns(isStale) loads the list and returns its runs (or null on
// failure). Polling restarts when deps change; isStale() is then true for
// a fetch the old round started, so it can leave the state alone.
export default function usePollRuns(fetchRuns, deps = []) {
  // The latest fetchRuns, so a new function each render doesn't restart
  // the polling. Declared first: it runs before the effect below.
  const fetchRef = useRef(fetchRuns);
  useEffect(() => {
    fetchRef.current = fetchRuns;
  });

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let inFlight = false;
    const hidden = () => document.visibilityState === 'hidden';

    const poll = async () => {
      timer = null;
      // Hidden: stop here; becoming visible starts again.
      if (cancelled || hidden() || inFlight) return;
      inFlight = true;
      let runs = null;
      try {
        runs = await fetchRef.current(() => cancelled);
      } catch {
        runs = null;
      } finally {
        inFlight = false;
      }
      if (cancelled || hidden()) return;
      timer = setTimeout(poll, hasLiveRun(runs) ? LIVE_POLL_MS : IDLE_POLL_MS);
    };

    const onVisibility = () => {
      if (hidden() || cancelled) return;
      // Back on the tab: read now rather than after a full interval.
      clearTimeout(timer);
      poll();
    };

    poll();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
