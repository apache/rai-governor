// Memory filtered to one run, batch review and undo (B11).
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import MemoryPage from './MemoryPage';
import memoryService from '../services/memoryService';

vi.mock('../services/memoryService', () => ({
  default: {
    guidanceRepos: vi.fn(() => Promise.resolve({ repos: [] })),
    domains: vi.fn(),
    search: vi.fn(),
    get: vi.fn(),
    history: vi.fn(),
    forRun: vi.fn(),
    batchReview: vi.fn(),
    undoBatch: vi.fn(),
    review: vi.fn(),
  },
}));

const RUN = 'ca5e9137-0000-4000-8000-00000000d7a6';
const hit = (id, type, extra = {}) => ({ id, title: id, type, trust_tier: 'raw', author: 'agent:x', created: '2026-11-10', run: RUN, ...extra });
const HITS = [
  hit('profile', 'convention'),
  hit('domains', 'method'),
  hit('finding-1', 'fact'),
  hit('finding-2', 'fact'),
  hit('a-decision', 'decision'),
];

const page = (route) => renderWithRouter(
  <Routes>
    <Route path="/memories/:domain" element={<MemoryPage />} />
  </Routes>,
  { route },
);

describe('Memory, one run', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    memoryService.domains.mockResolvedValue({
      domains: [{ domain: 'tooling', kind: 'project', canWrite: true, canReview: true, entries: 4, raw: 4, due: 0, review: 4 }],
      you: { author: 'human:asf:akm', canWrite: true, canReview: true },
    });
    memoryService.search.mockResolvedValue({ hits: HITS });
    memoryService.get.mockResolvedValue({ frontmatter: { id: 'profile', domain: 'tooling', author: 'agent:x', trust_tier: 'raw', run: RUN }, body: 'x' });
    memoryService.history.mockResolvedValue({ versions: [], reviews: [] });
    memoryService.forRun.mockResolvedValue({
      run: { runId: RUN, visible: true, agentName: 'asvs_orchestrate', project: 'tooling', status: 'success' },
      domains: [{ domain: 'tooling', entries: 4, live: 4 }],
    });
  });

  it('lists the run\'s entries with a band that links back to the run', async () => {
    page(`/memories/tooling?run=${RUN}`);
    expect(await screen.findByText(/asvs_orchestrate for tooling · finished · wrote 4 entries here/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open run on Workloads' })).toHaveAttribute('href', `/run/${RUN}`);
    await waitFor(() => expect(memoryService.search).toHaveBeenCalledWith('tooling', expect.objectContaining({ run: RUN, limit: 1000, includeInactive: true })));
    expect(await screen.findByRole('link', { name: 'ca5e9137' })).toHaveAttribute('href', `/run/${RUN}`);
  });

  it('"Demote its findings" ticks the findings and leaves the reusable entries', async () => {
    page(`/memories/tooling?run=${RUN}&select=findings`);
    expect(await screen.findByText('2 selected')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Select: finding-1' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select: profile' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select: a-decision' })).not.toBeChecked();
    expect(screen.getAllByText('Reusable: kept out of the batch')).toHaveLength(2);

    memoryService.batchReview.mockResolvedValue({ batch: 'b-1', action: 'demote', applied: ['finding-1', 'finding-2'], skipped: [], restored: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Demote 2 entries' }));
    await waitFor(() => expect(memoryService.batchReview).toHaveBeenCalledWith(expect.objectContaining({
      ids: ['finding-1', 'finding-2'], action: 'demote', restoreSuperseded: false,
    })));
    expect(await screen.findByText('Demoted 2 entries')).toBeInTheDocument();

    memoryService.undoBatch.mockResolvedValue({ batch: 'b-1', undone: ['finding-1', 'finding-2'], skipped: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Undo batch' }));
    expect(await screen.findByText('Undid 2 of the batch.')).toBeInTheDocument();
    expect(memoryService.undoBatch).toHaveBeenCalledWith('b-1');
  });

  it('needs a reason before anything but keep', async () => {
    page(`/memories/tooling?run=${RUN}`);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Select: finding-1' }));
    const reason = screen.getByLabelText('Why (recorded on each entry)');
    fireEvent.change(reason, { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Demote 1 entry' })).toBeDisabled();
    fireEvent.click(screen.getByRole('radio', { name: 'Keep raw' }));
    expect(screen.getByRole('button', { name: 'Keep raw 1 entry' })).toBeEnabled();
  });

  it('readers see no checkboxes or batch bar', async () => {
    memoryService.domains.mockResolvedValue({
      domains: [{ domain: 'tooling', kind: 'project', canWrite: false, canReview: false, entries: 4, raw: 4, due: 0, review: 4 }],
      you: { author: 'human:asf:bob', canWrite: false, canReview: false },
    });
    page(`/memories/tooling?run=${RUN}&select=findings`);
    expect(await screen.findByText('finding-1')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: 'Select: finding-1' })).not.toBeInTheDocument();
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });

  it('keys move, tick and decide', async () => {
    memoryService.review.mockResolvedValue({});
    page(`/memories/tooling?run=${RUN}`);
    await screen.findByText('finding-1');
    const list = screen.getByLabelText('Entries');
    fireEvent.keyDown(list, { key: 'j' });
    fireEvent.keyDown(list, { key: 'x' });
    expect(await screen.findByText('1 selected')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Select: domains' })).toBeChecked();
    // A demote needs a reason: refused until there is one.
    fireEvent.keyDown(list, { key: 'd' });
    expect(await screen.findByText(/Write the reason in the bar below first/)).toBeInTheDocument();
    expect(memoryService.review).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Why (recorded on each entry)'), { target: { value: 'wrong domain map' } });
    fireEvent.keyDown(list, { key: 'd' });
    await waitFor(() => expect(memoryService.review).toHaveBeenCalledWith('domains', { action: 'demote', why: 'wrong domain map' }));
  });
});
