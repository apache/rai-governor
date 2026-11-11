import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import RunMemoriesButton from './RunMemoriesButton';
import RunsHistoryList from './RunsHistoryList';
import memoryService from '../services/memoryService';

vi.mock('../services/memoryService', () => ({
  default: { isEnabled: vi.fn(), forRun: vi.fn() },
}));

function Where() {
  const loc = useLocation();
  return <div>at {loc.pathname}{loc.search}</div>;
}

const at = (ui) => render(
  <MemoryRouter initialEntries={['/agent/a1/runs/r1']}>
    <Routes>
      <Route path="/agent/:agentId/runs/:runId" element={ui} />
      <Route path="/memories/:domain" element={<Where />} />
    </Routes>
  </MemoryRouter>,
);

describe('RunMemoriesButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    memoryService.isEnabled.mockResolvedValue(true);
  });

  it('opens Memory filtered to the run when it wrote to one domain', async () => {
    memoryService.forRun.mockResolvedValue({ domains: [{ domain: 'tooling', entries: 160 }] });
    at(<RunMemoriesButton runId="run-uuid-1" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Memories from this run' }));
    expect(await screen.findByText('at /memories/tooling?run=run-uuid-1')).toBeInTheDocument();
  });

  it('offers a menu when it wrote to several', async () => {
    memoryService.forRun.mockResolvedValue({ domains: [
      { domain: 'tooling', entries: 2 }, { domain: 'shared-asvs', entries: 1 }] });
    at(<RunMemoriesButton runId="r9" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Memories from this run' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: /shared-asvs/ }));
    expect(await screen.findByText('at /memories/shared-asvs?run=r9')).toBeInTheDocument();
  });

  it('says so when the run wrote nothing the caller can read', async () => {
    memoryService.forRun.mockResolvedValue({ domains: [] });
    at(<RunMemoriesButton runId="r0" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Memories from this run' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Nothing from this run in memory you can read');
  });

  it('is not there when memory is off, and asks nothing until clicked', async () => {
    memoryService.isEnabled.mockResolvedValue(false);
    at(<RunMemoriesButton runId="r1" />);
    await waitFor(() => expect(memoryService.isEnabled).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Memories from this run' })).toBeNull();
    expect(memoryService.forRun).not.toHaveBeenCalled();
  });

  it('is on each saved-agent run row, and clicking it does not open the row', async () => {
    memoryService.forRun.mockResolvedValue({ domains: [{ domain: 'tooling', entries: 3 }] });
    const onOpen = vi.fn();
    const runs = [{ run_id: 'a', outcome: 'success', started_at: new Date().toISOString(), input: {} },
      { run_id: 'b', outcome: 'error', started_at: new Date().toISOString(), input: {} }];
    at(<RunsHistoryList runs={runs} inputSchema={{}} onOpen={onOpen} onRerun={() => {}} showMemories />);
    const buttons = await screen.findAllByRole('button', { name: 'Memories from this run' });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]);   // newest first: run b
    expect(await screen.findByText('at /memories/tooling?run=b')).toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('is not on rows of an unsaved agent', async () => {
    const runs = [{ run_id: 'run-123-abc', outcome: 'success', started_at: new Date().toISOString(), input: {} }];
    at(<RunsHistoryList runs={runs} inputSchema={{}} onOpen={() => {}} onRerun={() => {}} />);
    await waitFor(() => expect(screen.getByRole('button', { name: /re-run/i })).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Memories from this run' })).toBeNull();
  });
});
