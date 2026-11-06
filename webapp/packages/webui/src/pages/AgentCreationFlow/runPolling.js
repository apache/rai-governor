// How often the runs page re-reads run status, or null for not at all.
//
// The page used to poll every 5 s while anything on it was running, two
// requests a tick (the run and the agent's run list), even when the run in
// view was being followed over its stream, which reports completion itself.
// Now:
//   - the run in view, followed live: no polling for it
//   - the run in view, running but not followed (stream disconnected or
//     never opened): every 5 s, as before
//   - only other runs of this agent running: every 30 s, for the list
const LIVE = ['connecting', 'running', 'reconnecting'];

export const VIEWED_RUN_POLL_MS = 5000;
export const OTHER_RUNS_POLL_MS = 30000;

export function pollIntervalMs({ runId, streamStatus, fetchedRun, historicalRuns }) {
  const followed = Boolean(runId) && LIVE.includes(streamStatus);
  const viewedRunning =
    (fetchedRun && fetchedRun.status === 'running') ||
    (historicalRuns || []).some((r) => r.run_id === runId && r.outcome === 'running');
  if (viewedRunning && !followed) return VIEWED_RUN_POLL_MS;
  const othersRunning = (historicalRuns || []).some(
    (r) => r.outcome === 'running' && r.run_id !== runId,
  );
  return othersRunning ? OTHER_RUNS_POLL_MS : null;
}
