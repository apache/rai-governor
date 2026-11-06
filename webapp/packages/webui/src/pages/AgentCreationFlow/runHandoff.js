// ISSUE-024: when the streaming request that started a run drops, the run
// keeps going on the server. Instead of marking it failed, the page hands
// the run over to useRun, which reattaches to /runs/{id}/stream and replays
// it. These helpers do the bookkeeping on the page's local run list.

const TERMINAL = ['success', 'error', 'stopped'];

export const HANDOFF_MESSAGE =
  'Connection to the run dropped. The run is still going on the server; following it again.';

// Mark the in-flight (last, still running) entry as handed off to the
// stream of server run `runId`.
export function handOffRun(runs, runId) {
  if (runs.length === 0 || !runId) return runs;
  const last = runs[runs.length - 1];
  if (!last || last.outcome !== 'running' || last.handedOff) return runs;
  const next = [...runs];
  next[next.length - 1] = {
    ...last,
    handedOff: true,
    serverRunId: runId,
    events: [
      ...(last.events || []),
      {
        type: 'info',
        ts: new Date().toISOString(),
        agent_name: last.agent_name || 'unknown',
        depth: 0,
        message: HANDOFF_MESSAGE,
        source: 'system',
      },
    ],
  };
  return next;
}

// What the progress log shows: the entry handed off to the stream being
// followed (`followedRunId`) takes its events and outcome from that stream
// once it has delivered any. Other entries, including earlier hand-offs,
// are left as they are.
export function mergeHandedOffRuns(runs, streamedEvents, streamStatus, followedRunId, now = Date.now()) {
  if (!runs.some((r) => r && r.handedOff && r.serverRunId === followedRunId)) return runs;
  return runs.map((r) => {
    if (!r || !r.handedOff || r.serverRunId !== followedRunId) return r;
    const finished = TERMINAL.includes(streamStatus);
    return {
      ...r,
      events: streamedEvents.length > 0 ? streamedEvents : r.events,
      outcome: finished ? streamStatus : 'running',
      duration_ms: finished && r._started_ms ? now - r._started_ms : r.duration_ms,
    };
  });
}

// When the followed stream ends, write the result into the entry for good,
// so it stays put after the page moves on to another run.
export function finalizeHandedOffRun(runs, streamedEvents, streamStatus, followedRunId, now = Date.now()) {
  if (!TERMINAL.includes(streamStatus)) return runs;
  if (!runs.some((r) => r && r.handedOff && r.serverRunId === followedRunId)) return runs;
  return mergeHandedOffRuns(runs, streamedEvents, streamStatus, followedRunId, now).map((r) =>
    r && r.handedOff && r.serverRunId === followedRunId ? { ...r, handedOff: false } : r,
  );
}
