// The home page's run list asked GET /runs every 5 s whether anything was
// running or not, and in hidden tabs too.
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import RunningJobsModule from './RunningJobsModule';
import { listRuns } from '../services/runService';

vi.mock('../services/runService', () => ({
  listRuns: vi.fn(), stopRun: vi.fn(), forceCompleteRun: vi.fn(),
}));

let visibility = 'visible';
const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const run = (status) => ({
  runId: `r-${status}`, agentName: 'scan', status, startedAt: '2026-11-19T00:00:00',
});

describe('RunningJobsModule polling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  });

  afterEach(() => {
    vi.useRealTimers();
    delete document.visibilityState;
  });

  it('polls every 5 s while a run is live and every 60 s once none is', async () => {
    listRuns.mockResolvedValue({ runs: [run('running')] });
    render(<MemoryRouter><RunningJobsModule /></MemoryRouter>);
    await advance(0);
    await advance(5000);
    expect(listRuns).toHaveBeenCalledTimes(2);

    listRuns.mockResolvedValue({ runs: [run('success')] });
    await advance(5000);
    expect(listRuns).toHaveBeenCalledTimes(3);
    await advance(55000);
    expect(listRuns).toHaveBeenCalledTimes(3);
    await advance(5000);
    expect(listRuns).toHaveBeenCalledTimes(4);
  });

  it('does not poll in a hidden tab', async () => {
    listRuns.mockResolvedValue({ runs: [run('running')] });
    render(<MemoryRouter><RunningJobsModule /></MemoryRouter>);
    await advance(0);
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    await advance(120000);
    expect(listRuns).toHaveBeenCalledTimes(1);
  });
});
