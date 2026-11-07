// Memory page (ISSUE-026): domain, tabs, and the off state.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import MemoryPage from './MemoryPage';
import memoryService from '../services/memoryService';

vi.mock('../services/memoryService', () => ({
  default: {
    domains: vi.fn(),
    search: vi.fn(() => Promise.resolve({ hits: [] })),
    get: vi.fn(),
    history: vi.fn(),
  },
}));

const page = (route) => renderWithRouter(
  <Routes>
    <Route path="/memories" element={<MemoryPage />} />
    <Route path="/memories/:domain" element={<MemoryPage />} />
  </Routes>,
  { route },
);

const DOMAINS = {
  domains: [{ domain: 'dom-scan', entries: 4, raw: 2, due: 1, review: 3 }],
  you: { author: 'human:asf:bob', canWrite: true, canReview: true },
};

describe('MemoryPage', () => {
  beforeEach(() => vi.clearAllMocks());

  it('opens the first domain on Browse, with the review count on its tab', async () => {
    memoryService.domains.mockResolvedValue(DOMAINS);
    page('/memories');
    expect(await screen.findByRole('tab', { name: /Review/ })).toHaveTextContent('3');
    expect(screen.getByRole('tab', { name: 'Browse' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Nothing recorded in this domain yet.')).toBeInTheDocument();
    expect(memoryService.search).toHaveBeenCalledWith('dom-scan', expect.any(Object));
  });

  it('says so when memory is off', async () => {
    memoryService.domains.mockRejectedValue(Object.assign(new Error('Not Found'), { status: 404 }));
    page('/memories');
    expect(await screen.findByText('Memory is not enabled on this deployment.')).toBeInTheDocument();
  });

  it('names the domains there are when the URL names another', async () => {
    memoryService.domains.mockResolvedValue(DOMAINS);
    page('/memories/dom-nope');
    expect(await screen.findByText(/No memory domain dom-nope/)).toBeInTheDocument();
  });
});
