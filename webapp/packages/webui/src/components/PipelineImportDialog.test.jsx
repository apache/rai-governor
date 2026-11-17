// Import dialog (ISSUE-011): preview first, then import into the same copy.
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PipelineImportDialog from './PipelineImportDialog';
import pipelineService from '../services/pipelineService';

vi.mock('../services/pipelineService', () => ({
  default: { importFromGithub: vi.fn(), importFromFolder: vi.fn() },
}));

const plan = (extra = {}) => ({
  pipeline: 'asvs', label: '20261107-020000', entry: 'asvs_orchestrate',
  agents: [
    { agent: 'asvs_orchestrate', name: 'asvs_orchestrate@20261107-020000', action: 'create',
      changes: [], calls: ['asvs_audit'], externalCalls: [], envCount: 3 },
    { agent: 'asvs_audit', name: 'asvs_audit@20261107-020000', action: 'update',
      changes: ['code', 'env'], calls: [], externalCalls: [], envCount: 0 },
  ],
  problems: [],
  ...extra,
});

describe('PipelineImportDialog', () => {
  beforeEach(() => vi.clearAllMocks());

  it('previews a GitHub pipeline, then imports it', async () => {
    pipelineService.importFromGithub.mockResolvedValueOnce(plan({ label: '3194087', source: { repo: 'apache/tooling-agents', commit: '3194087abc' } }));
    pipelineService.importFromGithub.mockResolvedValueOnce({ entryId: 'e1' });
    const onImported = vi.fn();
    render(<PipelineImportDialog open onClose={() => {}} onImported={onImported} />);

    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'apache/tooling-agents' } });
    fireEvent.change(screen.getByLabelText(/Folder with pipeline.yaml/), { target: { value: 'ASVS' } });
    expect(screen.getByRole('button', { name: 'Import' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('code, env')).toBeInTheDocument();
    expect(screen.getByText(/apache\/tooling-agents 3194087/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith({ entryId: 'e1' }));
    expect(pipelineService.importFromGithub.mock.calls[1][0]).toMatchObject({
      repo: 'apache/tooling-agents', path: 'ASVS', dryRun: false, label: '3194087', deploy: true, labeled: true,
    });
  });

  it('unticking the label imports into the stable copy', async () => {
    pipelineService.importFromGithub.mockResolvedValueOnce(plan({
      label: '',
      agents: [{ agent: 'asvs_orchestrate', name: 'asvs_orchestrate', action: 'update',
        changes: ['code'], calls: [], externalCalls: [], envCount: 0 }],
    }));
    pipelineService.importFromGithub.mockResolvedValueOnce({ entryId: 'e1', deployed: [], deployProblems: [] });
    render(<PipelineImportDialog open onClose={() => {}} onImported={() => {}} />);
    const box = screen.getByRole('checkbox', { name: 'Label this copy' });
    expect(box).toBeChecked();
    fireEvent.change(screen.getByLabelText(/^Label$/), { target: { value: 'typed' } });
    fireEvent.click(box);
    expect(screen.queryByLabelText(/^Label$/)).not.toBeInTheDocument();
    expect(screen.getByText(/Imports into the stable copy/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'a/b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('asvs (stable)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(pipelineService.importFromGithub).toHaveBeenCalledTimes(2));
    for (const [args] of pipelineService.importFromGithub.mock.calls) {
      expect(args).toMatchObject({ labeled: false, label: undefined });
    }
  });

  it('deploys every agent unless unticked', async () => {
    pipelineService.importFromGithub.mockResolvedValueOnce(plan());
    pipelineService.importFromGithub.mockResolvedValueOnce({ entryId: 'e1', deployed: [], deployProblems: [] });
    render(<PipelineImportDialog open onClose={() => {}} onImported={() => {}} />);
    const box = screen.getByRole('checkbox', { name: /Deploy every agent/ });
    expect(box).toBeChecked();
    fireEvent.click(box);
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'a/b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('code, env');
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(pipelineService.importFromGithub).toHaveBeenCalledTimes(2));
    expect(pipelineService.importFromGithub.mock.calls[1][0].deploy).toBe(false);
  });

  it('says which agents were not deployed before closing', async () => {
    pipelineService.importFromGithub.mockResolvedValueOnce(plan());
    const result = {
      entryId: 'e1', written: { asvs_orchestrate: { id: 'e1' }, asvs_audit: { id: 'a1' } },
      deployed: [{ agent: 'asvs_orchestrate', endpoint: '/rest/asvs_orchestrate-x' }],
      deployProblems: [{ agent: 'asvs_audit', reason: "A deployment with the name 'asvs_audit-x' already exists for a different agent." }],
    };
    pipelineService.importFromGithub.mockResolvedValueOnce(result);
    const onImported = vi.fn();
    render(<PipelineImportDialog open onClose={() => {}} onImported={onImported} />);
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'a/b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('code, env');
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    expect(await screen.findByText(/deployed 1 of 2 agents/)).toBeInTheDocument();
    expect(screen.getByText(/already exists for a different agent/)).toBeInTheDocument();
    expect(onImported).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onImported).toHaveBeenCalledWith(result);
  });

  it('imports a folder into the copy it previewed, not a new timestamp', async () => {
    pipelineService.importFromFolder.mockResolvedValueOnce(plan());
    pipelineService.importFromFolder.mockResolvedValueOnce({ entryId: 'e2' });
    render(<PipelineImportDialog open onClose={() => {}} onImported={() => {}} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Folder' }));

    const file = new File(['pipeline: asvs'], 'pipeline.yaml');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'ASVS/pipeline.yaml' });
    fireEvent.change(screen.getByTestId('folder-input'), { target: { files: [file] } });
    expect(screen.getByText('ASVS: 1 files')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('asvs@20261107-020000');
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    await waitFor(() => expect(pipelineService.importFromFolder).toHaveBeenCalledTimes(2));
    expect(pipelineService.importFromFolder.mock.calls[0][0].label).toBeUndefined();
    expect(pipelineService.importFromFolder.mock.calls[1][0]).toMatchObject({ label: '20261107-020000', dryRun: false });
  });

  it('shows problems and will not import past them', async () => {
    pipelineService.importFromGithub.mockResolvedValueOnce(plan({ problems: ["asvs_audit: calls 'x', which is neither in the pipeline nor an existing agent"] }));
    render(<PipelineImportDialog open onClose={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'a/b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/calls 'x'/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import' })).toBeDisabled();
  });

  it('shows the API reason when a preview fails', async () => {
    pipelineService.importFromGithub.mockRejectedValueOnce(new Error('GitHub 404 for a/b@main'));
    render(<PipelineImportDialog open onClose={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/Repository/), { target: { value: 'a/b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText('GitHub 404 for a/b@main')).toBeInTheDocument();
  });
});
