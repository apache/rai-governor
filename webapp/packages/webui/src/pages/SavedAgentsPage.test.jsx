// The agent list marks which copy of a pipeline each agent belongs to.
import { describe, expect, it, vi } from 'vitest';
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
  it('marks the stable copy and labelled copies, and nothing on other agents', async () => {
    renderWithRouter(<SavedAgentsPage />);
    expect(await screen.findByText('stable')).toBeInTheDocument();
    expect(screen.getByText('@abc1234')).toBeInTheDocument();
    expect(screen.getAllByText(/^(stable|@.*)$/)).toHaveLength(2);
  });
});
