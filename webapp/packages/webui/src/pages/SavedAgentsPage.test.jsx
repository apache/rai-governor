// The agent list marks which copy of a pipeline each agent belongs to.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithRouter } from '../test/testUtils';
import SavedAgentsPage from './SavedAgentsPage';

vi.mock('../services/agentService', () => {
  const agents = [
    { _id: 's', name: 'asvs_audit', baseName: 'asvs_audit', pipeline: 'asvs', pipelineLabel: '', description: '' },
    { _id: 'l', name: 'asvs_audit@abc1234', baseName: 'asvs_audit', pipeline: 'asvs', pipelineLabel: 'abc1234', description: '' },
    { _id: 'm', name: 'hand_made', description: '' },
  ];
  return {
    default: {
      getAgents: vi.fn(() => Promise.resolve(agents)),
      withDeploymentStatus: vi.fn((a) => Promise.resolve(a)),
    },
  };
});

describe('SavedAgentsPage', () => {
  afterEach(() => vi.restoreAllMocks());

  it('nests nothing React warns about', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderWithRouter(<SavedAgentsPage />);
    await screen.findByText('stable');
    expect(errors.mock.calls.map((c) => String(c[0])).filter((m) => /cannot be a descendant|cannot contain/.test(m))).toEqual([]);
  });

  it('marks the stable copy and labelled copies, and nothing on other agents', async () => {
    renderWithRouter(<SavedAgentsPage />);
    expect(await screen.findByText('stable')).toBeInTheDocument();
    expect(screen.getByText('@abc1234')).toBeInTheDocument();
    expect(screen.getAllByText(/^(stable|@.*)$/)).toHaveLength(2);
  });
});
