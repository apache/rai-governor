// The runs page polls only what its stream does not already report.
import { describe, expect, it } from 'vitest';
import { OTHER_RUNS_POLL_MS, VIEWED_RUN_POLL_MS, pollIntervalMs } from './runPolling';

const running = (id) => ({ run_id: id, outcome: 'running' });
const done = (id) => ({ run_id: id, outcome: 'success' });

describe('pollIntervalMs', () => {
  it('does not poll a run that is followed over its stream', () => {
    for (const streamStatus of ['connecting', 'running', 'reconnecting']) {
      expect(pollIntervalMs({
        runId: 'r1', streamStatus,
        fetchedRun: { status: 'running' }, historicalRuns: [running('r1')],
      })).toBeNull();
    }
  });

  it('polls the run in view every 5 s when its stream is not live', () => {
    for (const streamStatus of ['disconnected', 'idle', 'error']) {
      expect(pollIntervalMs({
        runId: 'r1', streamStatus, fetchedRun: { status: 'running' }, historicalRuns: [],
      })).toBe(VIEWED_RUN_POLL_MS);
    }
  });

  it('polls slowly when only other runs are running', () => {
    expect(pollIntervalMs({
      runId: 'r1', streamStatus: 'running',
      fetchedRun: null, historicalRuns: [running('r1'), running('r2')],
    })).toBe(OTHER_RUNS_POLL_MS);
    expect(pollIntervalMs({
      runId: undefined, streamStatus: 'idle', fetchedRun: null, historicalRuns: [running('r2')],
    })).toBe(OTHER_RUNS_POLL_MS);
  });

  it('stops when nothing is running', () => {
    expect(pollIntervalMs({
      runId: 'r1', streamStatus: 'success',
      fetchedRun: { status: 'success' }, historicalRuns: [done('r1'), done('r2')],
    })).toBeNull();
  });

  it('polls a queued run like a running one', () => {
    expect(pollIntervalMs({
      runId: 'r1', streamStatus: 'disconnected', fetchedRun: { status: 'queued' }, historicalRuns: [],
    })).toBe(VIEWED_RUN_POLL_MS);
    expect(pollIntervalMs({
      runId: 'r1', streamStatus: 'running',
      fetchedRun: null, historicalRuns: [{ run_id: 'r2', outcome: 'queued' }],
    })).toBe(OTHER_RUNS_POLL_MS);
  });
});
