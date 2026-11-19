// The run page keeps what it shows while it reloads the run for its final
// status. It used to clear itself when the stream reported the end (which a
// finished run's stream does straight away), so every run page showed, went
// blank and showed again.
import React, { useEffect, useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import RunPage from './RunPage';
import workloadService from '../services/workloadService';

vi.mock('../services/workloadService', () => ({
  default: { getRun: vi.fn(), list: vi.fn(() => Promise.resolve({ workloads: [] })), stopRun: vi.fn() },
}));
vi.mock('../services/memoryService', () => ({
  default: { isEnabled: vi.fn(() => Promise.resolve(false)), forRun: vi.fn() },
}));
// The stream reports the run's end a moment after the page loads (unless
// fakeStream.ends is false).
const fakeStream = { ends: true };
function useFakeRun() {
  const [final, setFinal] = useState(null);
  useEffect(() => {
    if (!fakeStream.ends) return undefined;
    const t = setTimeout(() => setFinal({ type: 'final', status: 'success' }), 20);
    return () => clearTimeout(t);
  }, []);
  return { events: [], status: final ? 'success' : 'running', final, reconnect: () => {} };
}
vi.mock('../hooks/useRun', () => ({ default: useFakeRun }));

const RUN = {
  runId: 'f6c9b9a0-0000-4000-8000-000000000001', project: 'tooling', agentName: 'asvs_orchestrate',
  status: 'success', userId: 'asf:akm', startedAt: '2026-11-17T03:00:00', completedAt: '2026-11-17T05:00:00',
  inputDict: { sourceRepo: 'apache/rai-governor', commit: 'abc1234' },
};

describe('RunPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    fakeStream.ends = true;
    workloadService.getRun.mockResolvedValue(RUN);
  });

  it('keeps the page while it reloads the run at its end', async () => {
    renderWithRouter(<Routes><Route path="/run/:runId" element={<RunPage />} /></Routes>,
      { route: `/run/${RUN.runId}` });
    const heading = await screen.findByRole('heading', { level: 1 });
    await waitFor(() => expect(workloadService.getRun).toHaveBeenCalledTimes(2));
    await new Promise((r) => setTimeout(r, 30));
    // The same element: the page was never swapped for the spinner.
    expect(heading.isConnected).toBe(true);
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('shows a queued run\'s place, lets it be stopped, and notices when it starts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeStream.ends = false;
    const queued = { ...RUN, status: 'queued', queueAhead: 2, completedAt: null };
    workloadService.getRun.mockResolvedValueOnce(queued).mockResolvedValue({ ...queued, status: 'running', queueAhead: undefined });
    renderWithRouter(<Routes><Route path="/run/:runId" element={<RunPage />} /></Routes>,
      { route: `/run/${RUN.runId}` });
    expect(await screen.findByText('Queued — 2 ahead of you')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(5000);
    expect(await screen.findByText('Running for')).toBeInTheDocument();
    expect(screen.queryByText(/Queued/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
