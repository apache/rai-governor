import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AuthContext } from '../contexts/AuthContextValue';
import agentService from '../services/agentService';
import demoService from '../services/demoService';
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

  const at = (user) => render(
    <AuthContext.Provider value={{ user }}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/projects" element={<div>workloads page</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

  it('sends someone who cannot author to Workloads, without asking for agents or demos', async () => {
    agentService.getAgents.mockClear();
    demoService.getDemos.mockClear();
    at({ uid: 'asf:alice', canAuthor: false });
    expect(await screen.findByText('workloads page')).toBeInTheDocument();
    expect(agentService.getAgents).not.toHaveBeenCalled();
    expect(demoService.getDemos).not.toHaveBeenCalled();
  });

  it('shows authors the dashboard', async () => {
    at({ uid: 'asf:akm', canAuthor: true });
    expect(await screen.findByRole('button', { name: /^import$/i })).toBeInTheDocument();
    expect(screen.queryByText('workloads page')).toBeNull();
  });
});
