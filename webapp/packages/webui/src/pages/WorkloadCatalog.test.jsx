// Publishing to the catalog, and a PMC adding from it (B11).
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { Link, Route, Routes } from 'react-router-dom';
import { renderWithRouter } from '../test/testUtils';
import WorkloadsPage from './WorkloadsPage';
import WorkloadCatalogPage from './WorkloadCatalogPage';
import WorkloadEditorPage from './WorkloadEditorPage';
import DeployedApisPage from './DeployedApisPage';
import workloadService from '../services/workloadService';
import agentService from '../services/agentService';
import { badRepositoryLines, bodyFrom, parseRepositories, rowsFor } from '../components/workloads/publishForm';

vi.mock('../services/workloadService', () => ({
  default: {
    projects: vi.fn(), list: vi.fn(), runs: vi.fn(), run: vi.fn(),
    add: vi.fn(), setDefaults: vi.fn(), remove: vi.fn(),
    catalog: vi.fn(), publish: vi.fn(), update: vi.fn(), unpublish: vi.fn(), deployments: vi.fn(),
  },
}));
vi.mock('../services/agentService', () => ({ default: { getDeployments: vi.fn() } }));
vi.mock('../services/memoryService', () => ({
  default: { isEnabled: vi.fn(() => Promise.resolve(false)), runs: vi.fn() },
}));

const ASVS = {
  id: 'asvs-audit', name: 'ASVS audit', description: 'Audits one repository against OWASP ASVS.',
  inputs: [
    { name: 'sourceRepo', label: 'Repository', kind: 'repository', choices: ['apache/tooling-llmao', 'apache/tooling-agents'], default: 'apache/tooling-llmao', workloadDefault: null },
    { name: 'level', label: 'ASVS level', kind: 'choice', choices: ['L1', 'L2', 'L3'], default: 'L1', workloadDefault: 'L1' },
  ],
  fixedInputNames: ['sourceToken'], produces: 'Reports in apache/tooling-runbooks',
};

const DEPLOYMENT = {
  friendlyName: 'asvs_orchestrate', agentId: 'a1', description: 'ASVS pipeline',
  inputSchema: { sourceRepo: 'string', commit: 'string', level: 'string', sourceToken: 'string', outputRepo: 'string', discover: 'string' },
};

const pages = (route) => renderWithRouter(
  <>
  <nav>
    <Link to="/workload-catalog/new">go to new</Link>
    <Link to="/projects/beta/workloads">go to beta</Link>
  </nav>
  <Routes>
    <Route path="/projects/:project/workloads" element={<WorkloadsPage />} />
    <Route path="/workload-catalog" element={<WorkloadCatalogPage />} />
    <Route path="/workload-catalog/new" element={<WorkloadEditorPage />} />
    <Route path="/workload-catalog/:id" element={<WorkloadEditorPage />} />
  </Routes>
  </>,
  { route },
);

describe('the publish form helpers', () => {
  it('guesses who sets each input of a new workload', () => {
    const rows = Object.fromEntries(rowsFor(Object.keys(DEPLOYMENT.inputSchema)).map((r) => [r.name, r]));
    expect(rows.sourceToken.role).toBe('fixed');
    expect(rows.outputRepo.role).toBe('fixed');
    expect(rows.sourceRepo).toMatchObject({ role: 'member', kind: 'repository', required: true });
    expect(rows.commit).toMatchObject({ role: 'member', kind: 'text', required: false });
    expect(rows.discover.role).toBe('unused');
  });

  it('builds the body the API takes', () => {
    const rows = rowsFor(['sourceRepo', 'level', 'sourceToken', 'discover']).map((r) => (
      r.name === 'level' ? { ...r, role: 'member', kind: 'choice', choicesText: 'L1, L2, L3', default: 'L1' }
        : r.name === 'sourceToken' ? { ...r, value: 'github_pat_x' } : r));
    const body = bodyFrom({
      deployment: 'asvs_orchestrate', name: ' ASVS audit ', description: 'd', produces: '', typicalDuration: '',
      enabled: true, scope: 'some', projectsText: 'Tooling, whimsy', reposText: 'Tooling: apache/tooling-llmao, apache/tooling-agents\n\n', rows,
    });
    expect(body).toMatchObject({
      name: 'ASVS audit', deployment: 'asvs_orchestrate', projects: ['tooling', 'whimsy'],
      fixedInputs: { sourceToken: 'github_pat_x' },
      repositories: { tooling: ['apache/tooling-llmao', 'apache/tooling-agents'] },
      produces: null,
    });
    expect(body.inputs).toEqual([
      { name: 'sourceRepo', label: 'Repository', kind: 'repository', required: true },
      { name: 'level', label: 'Level', kind: 'choice', required: false, default: 'L1', choices: ['L1', 'L2', 'L3'] },
    ]);
    expect(parseRepositories('whimsy: apache/whimsy apache/whimsy-private')).toEqual({ whimsy: ['apache/whimsy', 'apache/whimsy-private'] });
  });

  it('refuses repository lines it cannot read, rather than dropping them', () => {
    expect(badRepositoryLines('tooling: apache/tooling\nnot a line\nwhimsy:\n')).toEqual(['not a line', 'whimsy:']);
    const form = { deployment: 'd', name: 'n', description: '', produces: '', typicalDuration: '', enabled: true,
      scope: 'all', projectsText: '', reposText: 'tooling apache/tooling', rows: [] };
    expect(() => bodyFrom(form)).toThrow(/can't read "tooling apache\/tooling"/);
  });

  it('sends fixed values back as the type they were', () => {
    const rows = rowsFor([], { inputs: [], fixedInputs: { n: 5, opts: { a: 1 }, tok: 'x' } });
    const form = { deployment: 'd', name: 'n', description: '', produces: '', typicalDuration: '', enabled: true,
      scope: 'all', projectsText: '', reposText: '', rows };
    expect(bodyFrom(form).fixedInputs).toEqual({ n: 5, opts: { a: 1 }, tok: 'x' });
    const broken = { ...form, rows: rows.map((r) => (r.name === 'n' ? { ...r, value: 'five' } : r)) };
    expect(() => bodyFrom(broken)).toThrow(/n: not valid JSON/);
  });
});

describe('WorkloadEditorPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workloadService.deployments.mockResolvedValue([DEPLOYMENT]);
  });

  it('starts from the deployment named in the link and publishes', async () => {
    workloadService.publish.mockResolvedValue({ id: 'asvs-orchestrate' });
    workloadService.catalog.mockResolvedValue({ workloads: [] });
    pages('/workload-catalog/new?deployment=asvs_orchestrate');
    expect(await screen.findByDisplayValue('Asvs orchestrate')).toBeInTheDocument();
    expect(screen.getByText('2 set by members, 2 fixed by you. Members never see fixed values; runs record only their names.')).toBeInTheDocument();
    const secret = screen.getByLabelText('Value (members never see it)');
    expect(secret).toHaveAttribute('type', 'password');
    fireEvent.change(secret, { target: { value: 'github_pat_x' } });
    fireEvent.change(screen.getByLabelText('Projects (LDAP names, comma-separated)'), { target: { value: 'tooling' } });
    fireEvent.click(screen.getByRole('button', { name: 'Publish to the catalog' }));
    await waitFor(() => expect(workloadService.publish).toHaveBeenCalled());
    const body = workloadService.publish.mock.calls[0][0];
    expect(body.projects).toEqual(['tooling']);
    expect(body.fixedInputs.sourceToken).toBe('github_pat_x');
    expect(body.inputs.map((i) => i.name)).toEqual(['sourceRepo', 'commit']);
    expect(await screen.findByText('Publish a workload')).toBeInTheDocument();   // back on the catalog
  });

  it('shows the API\'s refusal', async () => {
    workloadService.publish.mockRejectedValue(new Error('inputs › 0 › pattern: pattern does not compile'));
    pages('/workload-catalog/new?deployment=asvs_orchestrate');
    fireEvent.click(await screen.findByRole('button', { name: 'Publish to the catalog' }));
    expect(await screen.findByText('inputs › 0 › pattern: pattern does not compile')).toBeInTheDocument();
  });

  it('saving a change keeps the memory note and the types of fixed values', async () => {
    workloadService.catalog.mockResolvedValue({ workloads: [{
      id: 'a', name: 'A', deployment: 'asvs_orchestrate', projects: ['*'], inputs: [], enabled: true,
      fixedInputs: { sourceToken: 'TOPSECRET', n: 5 }, memoryNote: 'Findings go to the project', repositories: {},
    }] });
    workloadService.update.mockResolvedValue({});
    pages('/workload-catalog/a');
    expect(await screen.findByDisplayValue('Findings go to the project')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(workloadService.update).toHaveBeenCalled());
    const body = workloadService.update.mock.calls[0][1];
    expect(body.memoryNote).toBe('Findings go to the project');
    expect(body.fixedInputs).toEqual({ sourceToken: 'TOPSECRET', n: 5 });
  });

  it('going from one workload to a new one starts empty', async () => {
    workloadService.catalog.mockResolvedValue({ workloads: [{
      id: 'a', name: 'A', deployment: 'asvs_orchestrate', projects: ['*'], inputs: [], enabled: true,
      fixedInputs: { sourceToken: 'TOPSECRET' }, repositories: {},
    }] });
    pages('/workload-catalog/a');
    expect(await screen.findByRole('heading', { name: 'Change A' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('link', { name: 'go to new' }));
    expect(await screen.findByRole('heading', { name: 'Publish a workload' })).toBeInTheDocument();
    expect(screen.queryByDisplayValue('TOPSECRET')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('A')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish to the catalog' })).toBeDisabled();
  });

  it('says the catalog is for site admins', async () => {
    workloadService.deployments.mockRejectedValue(Object.assign(new Error('Forbidden'), { status: 403 }));
    pages('/workload-catalog/new');
    expect(await screen.findByText('Publishing workloads is for site administrators.')).toBeInTheDocument();
  });
});

describe('WorkloadCatalogPage', () => {
  beforeEach(() => vi.clearAllMocks());

  it('lists what is published and unpublishes', async () => {
    workloadService.catalog.mockResolvedValue({ workloads: [{
      id: 'asvs-audit', name: 'ASVS audit', deployment: 'asvs_orchestrate', projects: ['tooling'],
      inputs: [{ name: 'sourceRepo' }], fixedInputs: { sourceToken: 'x' }, enabled: true, publishedBy: 'asf:akm',
      addedTo: ['tooling'],
    }] });
    workloadService.unpublish.mockResolvedValue(null);
    pages('/workload-catalog');
    expect(await screen.findByText('1 by members · 1 fixed')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'On the page of' })).toBeInTheDocument();
    expect(screen.getAllByText('tooling')).toHaveLength(2);   // offered to, and on the page of
    expect(screen.getByRole('link', { name: 'Change' })).toHaveAttribute('href', '/workload-catalog/asvs-audit');
    fireEvent.click(screen.getByRole('button', { name: 'Unpublish' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Unpublish' }));
    await waitFor(() => expect(workloadService.unpublish).toHaveBeenCalledWith('asvs-audit'));
  });
});

describe('Adding a workload to a project', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workloadService.runs.mockResolvedValue({ runs: [] });
  });

  it('the PMC adds one from the catalog with the project\'s defaults', async () => {
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'admin' }], isSiteAdmin: true });
    workloadService.list.mockResolvedValueOnce({ project: 'tooling', canManage: true, workloads: [], available: [ASVS] });
    workloadService.add.mockResolvedValue({});
    pages('/projects/tooling/workloads');
    expect(await screen.findByText(/Nothing on tooling's page yet. One workload is available to add/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Workload catalog' })).toHaveAttribute('href', '/workload-catalog');
    fireEvent.click(screen.getByRole('button', { name: 'Add a workload' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Audits one repository against OWASP ASVS.')).toBeInTheDocument();
    workloadService.list.mockResolvedValue({ project: 'tooling', canManage: true, workloads: [{ ...ASVS, addedBy: 'asf:akm', projectDefaults: {} }], available: [] });
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: 'ASVS level' }));
    fireEvent.click(await screen.findByRole('option', { name: 'L2' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add to tooling' }));
    await waitFor(() => expect(workloadService.add).toHaveBeenCalledWith('asvs-audit', 'tooling', { level: 'L2' }));
    expect(await screen.findByRole('heading', { name: 'ASVS audit' })).toBeInTheDocument();
    expect(screen.getByText('asf:akm')).toBeInTheDocument();
  });

  it('the PMC changes defaults and removes', async () => {
    const COMMIT = { name: 'commit', label: 'Commit', kind: 'text', default: 'main', workloadDefault: null };
    const onPage = { ...ASVS, inputs: [...ASVS.inputs, COMMIT], projectDefaults: { commit: 'main' } };
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'admin' }], isSiteAdmin: false });
    workloadService.list.mockResolvedValue({ project: 'tooling', canManage: true, workloads: [onPage], available: [] });
    workloadService.setDefaults.mockResolvedValue({});
    workloadService.remove.mockResolvedValue(null);
    pages('/projects/tooling/workloads');
    fireEvent.click(await screen.findByRole('button', { name: 'Project defaults' }));
    let dialog = await screen.findByRole('dialog');
    const commit = within(dialog).getByLabelText('Commit');
    expect(commit).toHaveValue('main');
    // Blank means the workload's own default, which it doesn't have; not
    // the project's "main" shown back as the workload's.
    expect(commit).toHaveAttribute('placeholder', 'No default');
    expect(within(dialog).getByRole('combobox', { name: 'Repository' })).toHaveTextContent("The workload's: apache/tooling-llmao");
    fireEvent.change(commit, { target: { value: 'release' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save defaults' }));
    await waitFor(() => expect(workloadService.setDefaults).toHaveBeenCalledWith('asvs-audit', 'tooling', { commit: 'release' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Take it off' }));
    await waitFor(() => expect(workloadService.remove).toHaveBeenCalledWith('asvs-audit', 'tooling'));
    expect(screen.queryByRole('link', { name: 'Workload catalog' })).not.toBeInTheDocument();
  });

  it('committers see the modules but no Add, Remove or defaults', async () => {
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'member' }], isSiteAdmin: false });
    // Even with something to add, a committer gets no Add: it's the role.
    workloadService.list.mockResolvedValue({ project: 'tooling', canManage: false, workloads: [ASVS],
      available: [{ ...ASVS, id: 'other', name: 'Other' }] });
    pages('/projects/tooling/workloads');
    expect(await screen.findByRole('heading', { name: 'ASVS audit' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add a workload' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Project defaults' })).not.toBeInTheDocument();
  });

  it('an empty page tells committers who adds workloads', async () => {
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'member' }], isSiteAdmin: false });
    workloadService.list.mockResolvedValue({ project: 'tooling', canManage: false, workloads: [], available: [] });
    pages('/projects/tooling/workloads');
    expect(await screen.findByText("No workloads on tooling's page yet. tooling's PMC adds them from the workload catalog.")).toBeInTheDocument();
  });

  it('a slow answer for the project you left does not show under the next', async () => {
    workloadService.projects.mockResolvedValue({ projects: [{ project: 'tooling', role: 'admin' }, { project: 'beta', role: 'member' }], isSiteAdmin: false });
    let finishTooling;
    workloadService.list.mockImplementation((p) => (p === 'tooling'
      ? new Promise((ok) => { finishTooling = () => ok({ project: 'tooling', canManage: true, workloads: [ASVS], available: [] }); })
      : Promise.resolve({ project: 'beta', canManage: false, workloads: [], available: [] })));
    pages('/projects/tooling/workloads');
    await waitFor(() => expect(finishTooling).toBeDefined());
    fireEvent.click(screen.getByRole('link', { name: 'go to beta' }));
    await waitFor(() => expect(workloadService.list).toHaveBeenCalledWith('beta'));
    finishTooling();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('heading', { name: 'ASVS audit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument();
  });
});

describe('Publish as workload on Deployed APIs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    agentService.getDeployments.mockResolvedValue([DEPLOYMENT]);
  });

  it.each([[true, 1], [false, 0]])('site admin %s: %i buttons', async (isSiteAdmin, n) => {
    workloadService.projects.mockResolvedValue({ projects: [], isSiteAdmin });
    renderWithRouter(<DeployedApisPage />);
    expect(await screen.findByText('asvs_orchestrate')).toBeInTheDocument();
    await waitFor(() => expect(workloadService.projects).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryAllByText('Publish as workload', { exact: true })).toHaveLength(n);
  });
});
