// ISSUE-024: a dropped run stream hands the run to the reattached stream.
import { describe, expect, it } from 'vitest';
import { HANDOFF_MESSAGE, finalizeHandedOffRun, handOffRun, mergeHandedOffRuns } from './runHandoff';

const running = { run_id: 'r1', outcome: 'running', events: [{ type: 'start' }], _started_ms: 1000 };
const finished = { run_id: 'r0', outcome: 'success', events: [] };

describe('handOffRun', () => {
  it('marks the in-flight run with its server run id and notes the hand-off in its log', () => {
    const [prev, next] = handOffRun([finished, running], 'srv-1');
    expect(prev).toBe(finished);
    expect(next.handedOff).toBe(true);
    expect(next.serverRunId).toBe('srv-1');
    expect(next.outcome).toBe('running');
    expect(next.events.at(-1)).toMatchObject({ type: 'info', message: HANDOFF_MESSAGE });
  });

  it('leaves a finished run alone and does not hand off twice', () => {
    const list = [finished];
    expect(handOffRun(list, 'srv-1')).toBe(list);
    const once = handOffRun([running], 'srv-1');
    expect(handOffRun(once, 'srv-1')).toBe(once);
  });

  it('does nothing without a server run id to follow', () => {
    const list = [running];
    expect(handOffRun(list, null)).toBe(list);
  });
});

describe('mergeHandedOffRuns', () => {
  it('passes through when nothing was handed off', () => {
    const list = [finished, running];
    expect(mergeHandedOffRuns(list, [{ type: 'x' }], 'running', 'srv-1')).toBe(list);
  });

  it('shows the reattached stream once it has events, and keeps the run running', () => {
    const list = handOffRun([running], 'srv-1');
    const streamed = [{ type: 'start' }, { type: 'llm' }];
    const [r] = mergeHandedOffRuns(list, streamed, 'reconnecting', 'srv-1');
    expect(r.events).toBe(streamed);
    expect(r.outcome).toBe('running');
  });

  it('keeps the local log while the stream has not replayed yet', () => {
    const list = handOffRun([running], 'srv-1');
    const [r] = mergeHandedOffRuns(list, [], 'connecting', 'srv-1');
    expect(r.events).toBe(list[0].events);
  });

  it('takes the outcome and duration from the stream when the run ends', () => {
    const list = handOffRun([running], 'srv-1');
    const [r] = mergeHandedOffRuns(list, [{ type: 'done' }], 'success', 'srv-1', 6000);
    expect(r.outcome).toBe('success');
    expect(r.duration_ms).toBe(5000);
  });

  it('does not give a handed-off run the events of a different run', () => {
    // The page moved on to run srv-2; the srv-1 entry must keep its own log.
    const list = handOffRun([running], 'srv-1');
    const out = mergeHandedOffRuns(list, [{ type: 'other' }], 'success', 'srv-2');
    expect(out).toBe(list);
    expect(out[0].outcome).toBe('running');
  });
});

describe('finalizeHandedOffRun', () => {
  it('writes the finished outcome and events into the entry for good', () => {
    const list = handOffRun([running], 'srv-1');
    const streamed = [{ type: 'start' }, { type: 'done' }];
    const [r] = finalizeHandedOffRun(list, streamed, 'success', 'srv-1', 6000);
    expect(r).toMatchObject({ outcome: 'success', handedOff: false, duration_ms: 5000 });
    expect(r.events).toBe(streamed);
    // Following another run afterwards leaves it as it is.
    const [after] = mergeHandedOffRuns([r], [], 'connecting', 'srv-2');
    expect(after).toBe(r);
  });

  it('waits until the stream reports a terminal status', () => {
    const list = handOffRun([running], 'srv-1');
    expect(finalizeHandedOffRun(list, [{ type: 'x' }], 'running', 'srv-1')).toBe(list);
    expect(finalizeHandedOffRun(list, [{ type: 'x' }], 'success', 'srv-2')).toBe(list);
  });
});
