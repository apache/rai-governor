// Memory page (ISSUE-026): domain, tabs, and the off state.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import MemoryPage from './MemoryPage';
import memoryService from '../services/memoryService';

vi.mock('../services/memoryService', () => ({
  default: {
    guidanceRepos: vi.fn(() => Promise.resolve({ repos: [] })),
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

  it('takes review rights from the domain shown, not from the whole page', async () => {
    // ISSUE-002: a PMC member of one project reviews its domain only.
    memoryService.domains.mockResolvedValue({
      domains: [
        { domain: 'whimsy', kind: 'project', canWrite: false, canReview: false, entries: 1, raw: 1, due: 0, review: 1 },
        { domain: 'tomcat', kind: 'project', canWrite: true, canReview: true, entries: 0, raw: 0, due: 0, review: 0 },
      ],
      you: { author: 'human:asf:bob', canWrite: true, canReview: true },
    });
    memoryService.search.mockResolvedValue({ hits: [{ id: 'mem-whimsy-a1', summary: 'SQLi in board.rb', trust_tier: 'raw', author: 'agent:x', created: '2026-11-01' }] });
    memoryService.get.mockResolvedValue({ frontmatter: { id: 'mem-whimsy-a1', domain: 'whimsy', author: 'agent:x', trust_tier: 'raw', summary: 'SQLi in board.rb' }, body: '# SQLi' });
    memoryService.history.mockResolvedValue({ id: 'mem-whimsy-a1', domain: 'whimsy', versions: [], reviews: [] });
    page('/memories/whimsy?tab=review');
    expect(await screen.findByText("Only the project's PMC reviews entries in its domain.")).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toHaveTextContent('whimsy (project)');
  });

  it('names the domains there are when the URL names another', async () => {
    memoryService.domains.mockResolvedValue(DOMAINS);
    page('/memories/dom-nope');
    expect(await screen.findByText(/No memory domain dom-nope/)).toBeInTheDocument();
  });

  it('browses every readable domain together, read only', async () => {
    memoryService.domains.mockResolvedValue({
      domains: [
        { domain: 'whimsy', kind: 'project', canReview: true, entries: 2, raw: 1, due: 0, review: 1 },
        { domain: 'tomcat', kind: 'project', canReview: true, entries: 3, raw: 0, due: 0, review: 0 },
      ],
      you: { author: 'human:asf:bob', canWrite: true, canReview: true },
    });
    memoryService.search.mockResolvedValue({ hits: [{ id: 'mem-tomcat-a1', domain: 'tomcat', title: 'Open redirect', trust_tier: 'raw', type: 'fact', created: '2026-11-01' }] });
    memoryService.get.mockResolvedValue({ frontmatter: { id: 'mem-tomcat-a1', domain: 'tomcat', trust_tier: 'raw' }, body: '' });
    memoryService.history.mockResolvedValue({ id: 'mem-tomcat-a1', versions: [], reviews: [] });
    page('/memories/*?tab=review');
    expect(await screen.findByText('Open redirect')).toBeInTheDocument();
    expect(memoryService.search).toHaveBeenCalledWith('*', expect.any(Object));
    expect(screen.getAllByRole('combobox')[0]).toHaveTextContent('All my domains · 5 entries');
    expect(screen.getByText('tomcat')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /Review/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Ask' })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(memoryService.guidanceRepos).not.toHaveBeenCalled();
  });

  it('offers all domains only when there is more than one', async () => {
    memoryService.domains.mockResolvedValue(DOMAINS);
    page('/memories/*');
    expect(await screen.findByText(/No memory domain \*/)).toBeInTheDocument();
  });

  it('pages through more entries than one page holds, and selects every match', async () => {
    memoryService.domains.mockResolvedValue(DOMAINS);
    const hit = (i) => ({ id: `mem-${i}`, domain: 'dom-scan', title: `Finding ${i}`, trust_tier: 'raw', type: 'fact', created: '2026-11-01' });
    const range = (from, n) => Array.from({ length: n }, (_, i) => hit(from + i));
    memoryService.search.mockImplementation((d, o) => Promise.resolve({
      hits: range(o.offset || 0, Math.min(o.limit, 120 - (o.offset || 0))), total: 120,
    }));
    memoryService.get.mockResolvedValue({ frontmatter: { id: 'mem-0', trust_tier: 'raw' }, body: '' });
    memoryService.history.mockResolvedValue({ id: 'mem-0', versions: [], reviews: [] });
    page('/memories/dom-scan');
    expect(await screen.findByText('Finding 49')).toBeInTheDocument();
    expect(screen.queryByText('Finding 50')).not.toBeInTheDocument();
    expect(screen.getByText('Select all 50 shown of 120')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show 50 more · 50 of 120 shown' }));
    expect(await screen.findByText('Finding 99')).toBeInTheDocument();
    expect(memoryService.search).toHaveBeenLastCalledWith('dom-scan', expect.objectContaining({ offset: 50, limit: 50 }));
    fireEvent.click(screen.getByRole('button', { name: 'Select all 120 matches' }));
    expect(await screen.findByText('120 selected')).toBeInTheDocument();
  });

  it('says there is nothing to read when no domain is readable', async () => {
    memoryService.domains.mockResolvedValue({ domains: [], you: { author: 'human:asf:eve' } });
    page('/memories');
    expect(await screen.findByText(/no memory you can read yet/)).toBeInTheDocument();
    expect(screen.queryByText(/No memory domain/)).not.toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  });
});
