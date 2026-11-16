// Triage on a run's findings (scans that remember).
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
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
    undoBatch: vi.fn(),
    dispose: vi.fn(),
    disposeBatch: vi.fn(),
    acceptProposals: vi.fn(),
    rejectProposals: vi.fn(),
    digest: vi.fn(),
    acceptDigest: vi.fn(),
  },
}));

const RUN = '3f2a9c1e-0000-4000-8000-000000000001';
const REPO = 'apache/tooling-trusted-releases';
const finding = (id, title, evidence, extra = {}) => ({
  id, title, type: 'fact', trust_tier: 'raw', author: 'agent:asvs_consolidate (model: q)', created: '2026-11-12',
  run: RUN, origin: `asvs:finding:${REPO}`, evidence, ...extra,
});
const F35 = finding('mem-t-035', 'PubSub URL not validated', {
  finding: { global_id: 'FINDING-035', severity: 'Low', file: 'atr/pubsub.py', commit: '99599ab' },
  matches: [{ relation: 'fixed', ref: '#1683', title: 'Require secure transport', confidence: 'high' }],
  patch: { status: 'false_positive', reason: '_configured() already rejects it' },
  suggested: { kind: 'fp_context', why: 'fixed in #1683', guidance: 'PubSub transport is enforced in atr/pubsub.py.' },
});
const F10 = finding('mem-t-010', 'No PAT expiry reminder', {
  finding: { global_id: 'FINDING-010', severity: 'Low' },
  matches: [{ relation: 'declined', ref: '#1657' }],
  patch: { status: 'skipped_not_planned' },
});
const F01 = finding('mem-t-001', 'OIDC nonce missing', {
  finding: { global_id: 'FINDING-001', severity: 'Medium', file: 'src/asfquart/generics.py' },
  patch: { status: 'unlocated' },
});
const DONE = finding('mem-t-006', 'mfa_enabled no-op', { finding: { global_id: 'FINDING-006' } }, {
  status: 'demoted', disposition: 'duplicate',
  decision: { kind: 'duplicate', why: 'same flag', by: 'human:asf:akm', duplicate_of: 'FINDING-005' },
});
const PROP = {
  id: 'mem-t-p1', title: 'PAT reminders were declined', summary: 'PAT reminders were declined', type: 'decision',
  trust_tier: 'raw', author: 'agent:asvs_match (model: q)', run: RUN, origin: `proposal:guidance:${REPO}`,
};

const page = (route) => renderWithRouter(
  <Routes><Route path="/memories/:domain" element={<MemoryPage />} /></Routes>,
  { route },
);

describe('Triage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    memoryService.guidanceRepos.mockResolvedValue({ repos: [] });
    memoryService.domains.mockResolvedValue({
      domains: [{ domain: 'tooling', kind: 'project', canWrite: true, canReview: true, entries: 5, raw: 5, due: 0, review: 5 }],
      you: { author: 'human:asf:akm', canWrite: true, canReview: true },
    });
    memoryService.search.mockResolvedValue({ hits: [F35, F10, F01, DONE, PROP] });
    memoryService.get.mockImplementation((id) => Promise.resolve({ frontmatter: { id }, body: `body of ${id}` }));
    memoryService.forRun.mockResolvedValue({ run: { runId: RUN, visible: true, agentName: 'asvs_orchestrate', status: 'success' }, domains: [] });
    memoryService.digest.mockResolvedValue({ repo: REPO, current: null, draft: null, diff: null, pending: 3 });
    memoryService.history.mockResolvedValue({ versions: [], reviews: [] });
  });

  it('is a tab only when the page is filtered to a run', async () => {
    page('/memories/tooling?tab=triage');
    expect(await screen.findByRole('tab', { name: 'Browse' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('tab', { name: 'Triage' })).not.toBeInTheDocument();
  });

  it('counts the findings by what they need and shows the pipeline\'s evidence', async () => {
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    expect(await screen.findByRole('tab', { name: 'Needs a decision · 3' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Has a suggestion · 1' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Proposals · 1' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Decided · 1' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Files not found · 1' })).toBeInTheDocument();
    await waitFor(() => expect(memoryService.search).toHaveBeenCalledWith('tooling', expect.objectContaining({ run: RUN, includeInactive: true })));
    const list = screen.getByRole('generic', { name: 'Findings' });
    expect(within(list).getByText('#1683 closed, fixed')).toBeInTheDocument();
    expect(within(list).getByText('Patch: disputed')).toBeInTheDocument();
    expect(within(list).getByText('Suggested: false positive, the scanner lacked context')).toBeInTheDocument();
    expect(within(list).getByText('#1657 closed, not planned')).toBeInTheDocument();
    // The digest panel loads on its own; wait for it.
    expect(await screen.findByText(/3 decisions so far; run the guidance digest workload/)).toBeInTheDocument();
  });

  it('a suggestion fills the form in, and nothing is decided until it is recorded', async () => {
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    const form = await screen.findByRole('form', { name: 'Decide on this finding' });
    expect(within(form).getByRole('radio', { name: /the scanner lacked context/ })).toBeChecked();
    expect(within(form).getByLabelText(/Guidance for later scans/)).toHaveValue('PubSub transport is enforced in atr/pubsub.py.');
    expect(memoryService.dispose).not.toHaveBeenCalled();

    memoryService.dispose.mockResolvedValue({ batch: 'd-1', kind: 'fp_context', applied: ['mem-t-035'], skipped: [], guidance: ['mem-t-g1'] });
    fireEvent.click(within(form).getByRole('checkbox', { name: /Always use/ }));
    fireEvent.click(within(form).getByRole('button', { name: 'Record + guidance' }));
    await waitFor(() => expect(memoryService.dispose).toHaveBeenCalledWith('mem-t-035', {
      kind: 'fp_context', why: 'fixed in #1683', duplicateOf: undefined, filedAs: undefined,
      guidance: { text: 'PubSub transport is enforced in atr/pubsub.py.', scope: 'all', always: true },
    }));
    expect(await screen.findByText(/Recorded false positive, the scanner lacked context for 1 finding, and wrote 1 guidance entry/)).toBeInTheDocument();

    memoryService.undoBatch.mockResolvedValue({ batch: 'd-1', undone: ['mem-t-035'], skipped: [], withdrawn: ['mem-t-g1'] });
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(await screen.findByText('Undid 1 and withdrew 1 guidance entry.')).toBeInTheDocument();
  });

  it('one disposition for several findings, with one guidance entry', async () => {
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    await screen.findByRole('form', { name: 'Decide on this finding' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select: PubSub URL not validated' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select: OIDC nonce missing' }));
    const form = await screen.findByRole('form', { name: 'Decide on 2 findings' });
    fireEvent.click(within(form).getByRole('radio', { name: /By design/ }));
    expect(within(form).getByRole('button', { name: 'Record for 2 + guidance' })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText(/one entry citing all 2/), { target: { value: 'Declined.' } });
    fireEvent.change(within(form).getByLabelText(/^Why/), { target: { value: 'the project declined these' } });
    memoryService.disposeBatch.mockResolvedValue({ batch: 'd-2', kind: 'by_design', applied: ['mem-t-035', 'mem-t-001'], skipped: [], guidance: ['g'] });
    fireEvent.click(within(form).getByRole('button', { name: 'Record for 2 + guidance' }));
    await waitFor(() => expect(memoryService.disposeBatch).toHaveBeenCalledWith(['mem-t-035', 'mem-t-001'],
      expect.objectContaining({ kind: 'by_design', why: 'the project declined these', guidance: { text: 'Declined.', scope: 'all', always: false } })));
  });

  it('a decided finding shows the decision, with no form', async () => {
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    fireEvent.click(await screen.findByRole('tab', { name: 'Decided · 1' }));
    expect(await screen.findByText(/Decided: duplicate/, { selector: 'b' })).toBeInTheDocument();
    expect(screen.getByText(/duplicate of FINDING-005/)).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
  });

  it('proposals are accepted as the person\'s guidance, or rejected with a reason', async () => {
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    fireEvent.click(await screen.findByRole('tab', { name: 'Proposals · 1' }));
    expect(await screen.findByText('body of mem-t-p1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reject…' }));
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    memoryService.acceptProposals.mockResolvedValue({ batch: 'p-1', accepted: [{ proposal: 'mem-t-p1', guidance: 'g' }], skipped: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(memoryService.acceptProposals).toHaveBeenCalledWith(['mem-t-p1'], { edits: {} }));
    expect(await screen.findByText('Accepted: written as your guidance')).toBeInTheDocument();
  });

  it('a new digest version shows what it changes, and loads only once accepted', async () => {
    memoryService.digest.mockResolvedValue({
      repo: REPO, current: { id: 'd1', created: '2026-11-10T00:00:00Z' }, pending: 0,
      draft: { id: 'd2', body: '- A\n- C\n' }, diff: { added: ['- C'], removed: ['- B'], kept: ['- A'] },
    });
    page(`/memories/tooling?run=${RUN}&tab=triage`);
    expect(await screen.findByText('A new version is waiting: 1 added, 1 removed, 1 unchanged')).toBeInTheDocument();
    expect(screen.getByText('Added: C')).toBeInTheDocument();
    expect(screen.getByText('Removed: B')).toBeInTheDocument();
    memoryService.acceptDigest.mockResolvedValue({ accepted: 'd2' });
    fireEvent.click(screen.getByRole('button', { name: 'Accept this version' }));
    await waitFor(() => expect(memoryService.acceptDigest).toHaveBeenCalledWith('d2'));
  });
});
