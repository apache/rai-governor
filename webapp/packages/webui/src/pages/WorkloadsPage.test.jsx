// Workloads and run pages (B11).
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import WorkloadsPage from './WorkloadsPage';
import RunPage from './RunPage';
import workloadService from '../services/workloadService';
import memoryService from '../services/memoryService';

vi.mock('../services/workloadService', () => ({
  default: {
    projects: vi.fn(),
    list: vi.fn(),
    run: vi.fn(),
    runs: vi.fn(),
    getRun: vi.fn(),
    stopRun: vi.fn(),
  },
}));
vi.mock('../services/memoryService', () => ({
  default: { isEnabled: vi.fn(() => Promise.resolve(true)), runs: vi.fn(), forRun: vi.fn() },
}));
vi.mock('../hooks/useRun', () => ({
  default: () => ({ events: [{ type: 'stdout', message: 'step 1' }], status: 'running', final: null, reconnect: vi.fn() }),
}));

const ASVS = {
  id: 'asvs-audit',
  name: 'ASVS audit',
  description: 'Audits one repository against OWASP ASVS.',
  inputs: [
    { name: 'sourceRepo', label: 'Repository', kind: 'repository', choices: ['apache/tooling-llmao', 'apache/tooling-agents'], default: 'apache/tooling-llmao' },
    { name: 'commit', label: 'Commit', kind: 'text', default: 'latest' },
    { name: 'level', label: 'ASVS level', kind: 'choice', choices: ['L1', 'L2', 'L3'], default: 'L1' },
  ],
  fixedInputNames: ['outputRepo', 'sourceToken'],
  produces: 'Reports in apache/tooling-runbooks',
};

const RUN = {
  runId: 'ca5e9137-0000-4000-8000-00000000d7a6', workloadId: 'asvs-audit', project: 'tooling',
  agentName: 'asvs_orchestrate', status: 'error', error: 'consolidation crashed', userId: 'asf:akm',
  startedAt: '2026-11-09T18:40:00', completedAt: '2026-11-10T00:54:00',
  inputDict: { sourceRepo: 'apache/tooling-llmao', commit: '9445a24', level: 'L3', sourceToken: '(set by a site admin)' },
};

const routes = (route) => renderWithRouter(
  <Routes>
    <Route path="/projects" element={<WorkloadsPage />} />
    <Route path="/projects/:project/workloads" element={<WorkloadsPage />} />
    <Route path="/run/:runId" element={<RunPage />} />
    <Route path="/memories/:domain" element={<div>memory page</div>} />
  </Routes>,
  { route },
);

describe('WorkloadsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'admin' }, { project: 'whimsy', role: 'member' }] });
    workloadService.list.mockResolvedValue({ project: 'tooling', role: 'admin', workloads: [ASVS] });
    workloadService.runs.mockResolvedValue({ runs: [RUN] });
    memoryService.runs.mockResolvedValue({ runs: [{ run: RUN.runId, entries: 5 }] });
  });

  it('opens the first project and shows a module per workload with its runs', async () => {
    routes('/projects');
    expect(await screen.findByRole('heading', { name: 'ASVS audit' })).toBeInTheDocument();
    expect(workloadService.list).toHaveBeenCalledWith('tooling');
    expect(screen.getByText('Set by a site admin: outputRepo, sourceToken.')).toBeInTheDocument();
    expect(screen.getByText('tooling-llmao @ 9445a24')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open run' })).toHaveAttribute('href', `/run/${RUN.runId}`);
    expect(await screen.findByRole('link', { name: 'Memories (5)' })).toHaveAttribute('href', `/memories/tooling?run=${RUN.runId}`);
    expect(memoryService.runs).toHaveBeenCalledWith('tooling', 200);
  });

  it('runs the workload with the inputs shown, then opens the run', async () => {
    workloadService.run.mockResolvedValue({ runId: 'new-run' });
    workloadService.getRun.mockResolvedValue({ ...RUN, runId: 'new-run', status: 'running', error: null });
    memoryService.forRun.mockResolvedValue({ run: { runId: 'new-run', visible: true }, domains: [] });
    routes('/projects/tooling/workloads');
    fireEvent.click(await screen.findByRole('button', { name: 'Run for tooling' }));
    await waitFor(() => expect(workloadService.run).toHaveBeenCalledWith('asvs-audit', 'tooling', {
      sourceRepo: 'apache/tooling-llmao', commit: 'latest', level: 'L1',
    }));
    expect(await screen.findByText('step 1')).toBeInTheDocument();
  });

  it('shows why a run was refused', async () => {
    workloadService.run.mockRejectedValue(new Error('apache/tomcat is not one of tooling\'s repositories'));
    routes('/projects/tooling/workloads');
    fireEvent.click(await screen.findByRole('button', { name: 'Run for tooling' }));
    expect(await screen.findByText("apache/tomcat is not one of tooling's repositories")).toBeInTheDocument();
  });
});

describe('RunPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workloadService.getRun.mockResolvedValue(RUN);
    workloadService.list.mockResolvedValue({ workloads: [ASVS] });
    memoryService.forRun.mockResolvedValue({
      run: { runId: RUN.runId, visible: true },
      domains: [{ domain: 'tooling', entries: 5, live: 5, canReview: true, by_type: { convention: 2, method: 1, fact: 2 } }],
    });
  });

  it('says what went wrong and links to the memories it wrote', async () => {
    routes(`/run/${RUN.runId}`);
    expect(await screen.findByText('Failed: consolidation crashed')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'ASVS audit of tooling-llmao @ 9445a24' })).toBeInTheDocument();
    expect(screen.getByText('(set by a site admin)')).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Review in Memory' })).toHaveAttribute('href', `/memories/tooling?run=${RUN.runId}`);
    expect(screen.getByRole('link', { name: 'Demote its findings…' })).toHaveAttribute('href', `/memories/tooling?run=${RUN.runId}&select=findings`);
    expect(screen.getByText('step 1')).toBeInTheDocument();
  });

  it('offers no demote to someone who only reads the domain', async () => {
    memoryService.forRun.mockResolvedValue({
      run: { runId: RUN.runId, visible: true },
      domains: [{ domain: 'tooling', entries: 2, live: 2, canReview: false, by_type: { fact: 2 } }],
    });
    routes(`/run/${RUN.runId}`);
    expect(await screen.findByRole('link', { name: 'Review in Memory' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Demote its findings…' })).not.toBeInTheDocument();
  });

  it('runs again with the same inputs', async () => {
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'admin' }] });
    workloadService.runs.mockResolvedValue({ runs: [] });
    memoryService.runs.mockResolvedValue({ runs: [] });
    routes(`/run/${RUN.runId}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Run again' }));
    expect(await screen.findByRole('heading', { name: 'ASVS audit' })).toBeInTheDocument();
    const commit = await screen.findByLabelText('Commit');
    expect(commit).toHaveValue('9445a24');
  });
});
