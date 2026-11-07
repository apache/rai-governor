// Ask (ISSUE-026): a cited brief, or a plain "nothing matches".
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { renderWithRouter } from '../../test/testUtils';
import AskTab from './AskTab';
import memoryService from '../../services/memoryService';

vi.mock('../../services/memoryService', () => ({ default: { recall: vi.fn() } }));

const ask = (q) => {
  fireEvent.change(screen.getByPlaceholderText(/Ask what the record says/), { target: { value: q } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
};

describe('AskTab', () => {
  beforeEach(() => vi.clearAllMocks());

  it('links each citation and warns about unreviewed entries', async () => {
    memoryService.recall.mockResolvedValue({
      brief: 'The sort field is concatenated [mem-scan-k3v9q2xa].\nUnreviewed: mem-scan-k3v9q2xa',
      cited: ['mem-scan-k3v9q2xa'],
      entries: [{ id: 'mem-scan-k3v9q2xa', summary: 'String-built SQL', trust_tier: 'raw', type: 'fact' }],
      matched: 1,
    });
    renderWithRouter(<AskTab domain="dom-scan" />);
    ask('order report');
    expect(await screen.findByRole('button', { name: 'mem-scan-k3v9q2xa' })).toBeInTheDocument();
    expect(screen.getByText(/Drawn partly from unreviewed entries/)).toBeInTheDocument();
    expect(screen.queryByText(/^Unreviewed:/)).not.toBeInTheDocument();
    expect(memoryService.recall).toHaveBeenCalledWith('dom-scan', 'order report');
  });

  it('says plainly when nothing matched and no model was called', async () => {
    memoryService.recall.mockResolvedValue({ brief: '', cited: [], entries: [], matched: 0 });
    renderWithRouter(<AskTab domain="dom-scan" />);
    ask('kubernetes');
    expect(await screen.findByText(/No model was called/)).toBeInTheDocument();
  });
});
