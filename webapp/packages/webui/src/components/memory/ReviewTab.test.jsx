// Review queue (ISSUE-026): an author can't review their own entry.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/testUtils';
import ReviewTab from './ReviewTab';
import memoryService from '../../services/memoryService';
import { choicesFor } from './memoryFormat';

vi.mock('../../services/memoryService', () => ({
  default: { search: vi.fn(), get: vi.fn(), history: vi.fn(), review: vi.fn() },
}));

const hit = (id, extra = {}) => ({
  id, title: `Title ${id}`, summary: `Summary ${id}`, type: 'fact', trust_tier: 'raw',
  author: 'agent:asvs_audit (model: qwen)', status: null, created: '2026-11-03T00:00:00Z',
  review_by: '2099-01-01', ...extra,
});

const BOB = { author: 'human:asf:bob', canReview: true };

describe('ReviewTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    memoryService.get.mockImplementation(async (id) => ({
      frontmatter: { id, title: `Title ${id}`, trust_tier: 'raw', type: 'fact', author: 'x', created: '2026-11-03T00:00:00Z' },
      body: 'body',
    }));
    memoryService.history.mockResolvedValue({ versions: [], reviews: [] });
  });

  it('queues raw and overdue entries, oldest first, and leaves the rest out', async () => {
    memoryService.search.mockResolvedValue({ hits: [
      hit('mem-scan-a'),
      hit('mem-scan-b', { trust_tier: 'curated', review_by: '2000-01-01' }),
      hit('mem-scan-c', { trust_tier: 'verified' }),
    ] });
    renderWithRouter(<ReviewTab domain="dom-scan" you={BOB} />);
    expect(await screen.findByText('Summary mem-scan-a')).toBeInTheDocument();
    expect(screen.getByText('Summary mem-scan-b')).toBeInTheDocument();
    expect(screen.queryByText('Summary mem-scan-c')).not.toBeInTheDocument();
    expect(memoryService.search).toHaveBeenCalledWith('dom-scan', { order: 'oldest', queue: true, limit: 1000 });
    expect(screen.getByText('Raw · 1')).toBeInTheDocument();
    expect(screen.getByText('Review overdue · 1')).toBeInTheDocument();
  });

  it('records a curate with its why, then moves to the next entry', async () => {
    memoryService.search.mockResolvedValue({ hits: [hit('mem-scan-a'), hit('mem-scan-b')] });
    memoryService.review.mockResolvedValue({});
    renderWithRouter(<ReviewTab domain="dom-scan" you={BOB} />);
    const button = await screen.findByRole('button', { name: 'Curate entry' });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Why/), { target: { value: 'read a.py at HEAD' } });
    fireEvent.click(button);
    await waitFor(() => expect(memoryService.review).toHaveBeenCalledWith(
      'mem-scan-a', expect.objectContaining({ action: 'curate', why: 'read a.py at HEAD' }),
    ));
    await waitFor(() => expect(memoryService.get).toHaveBeenLastCalledWith('mem-scan-b'));
  });

  it('will not let the author review their own entry', async () => {
    memoryService.search.mockResolvedValue({ hits: [hit('mem-scan-a', { author: BOB.author })] });
    renderWithRouter(<ReviewTab domain="dom-scan" you={BOB} />);
    expect(await screen.findByText(/someone else has to review it/)).toBeInTheDocument();
    expect(screen.getByText('You wrote this')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Curate entry' })).toBeDisabled();
  });

  it('offers only decisions that fit the tier', () => {
    expect(choicesFor('raw').map((c) => c.action)).toEqual(['curate', 'verify', 'keep', 'demote']);
    expect(choicesFor('curated').map((c) => c.action)).toEqual(['keep', 'verify', 'demote']);
    expect(choicesFor('verified').map((c) => c.action)).toEqual(['keep', 'demote']);
  });

  it('an entry opened by link that is not in the queue still gets a decision', async () => {
    memoryService.search.mockResolvedValue({ hits: [hit('mem-scan-a')] });
    memoryService.get.mockImplementation(async (id) => ({
      frontmatter: { id, title: `Title ${id}`, trust_tier: 'curated', type: 'fact',
        author: 'agent:asvs_audit (model: qwen)', created: '2026-11-03T00:00:00Z', review_by: '2099-01-01' },
      body: 'body',
    }));
    renderWithRouter(<ReviewTab domain="dom-scan" you={BOB} initialEntry="mem-scan-linked" />);
    expect(await screen.findByText('Your decision')).toBeInTheDocument();
    expect(memoryService.get).toHaveBeenCalledWith('mem-scan-linked');
  });
});
