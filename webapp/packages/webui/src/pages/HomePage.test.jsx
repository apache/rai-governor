import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import HomePage from './HomePage';

vi.mock('../components/RunningJobsModule', () => ({ default: () => null }));
vi.mock('../services/agentService', () => ({
  default: { getAgents: vi.fn(() => Promise.resolve([])), getDeployment: vi.fn() },
}));
vi.mock('../services/demoService', () => ({
  default: { getDemos: vi.fn(() => Promise.resolve([])) },
}));
vi.mock('../services/dataStoreService', () => ({
  default: { listNamespaces: vi.fn(() => Promise.resolve({ namespaces: [] })) },
}));
vi.mock('../components/PipelineImportDialog', () => ({
  default: ({ open }) => (open ? <div role="dialog">import dialog</div> : null),
}));

describe('HomePage', () => {
  // /agents is also an API prefix, so the Saved Agents page can't be opened
  // by URL in production; import has to be reachable from here.
  it('opens the pipeline import dialog from the Agents card', async () => {
    render(<MemoryRouter><HomePage /></MemoryRouter>);
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^import$/i }));
    expect(screen.getByRole('dialog')).toHaveTextContent('import dialog');
  });
});
