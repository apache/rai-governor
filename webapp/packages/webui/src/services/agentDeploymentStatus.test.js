import { describe, it, expect, vi, beforeEach } from 'vitest';
import agentService from './agentService';

describe('agentService.withDeploymentStatus', () => {
  beforeEach(() => { vi.restoreAllMocks(); });

  it('marks a list of agents from one deployments call, not one per agent', async () => {
    const list = vi.spyOn(agentService, 'getDeployments').mockResolvedValue([
      { friendlyName: 'asvs_orchestrate', agentId: 'a1' },
    ]);
    const one = vi.spyOn(agentService, 'getDeployment');
    const out = await agentService.withDeploymentStatus([{ _id: 'a1', name: 'x' }, { _id: 'a2', name: 'y' }]);
    expect(out).toEqual([
      { _id: 'a1', name: 'x', isDeployed: true, deployedName: 'asvs_orchestrate' },
      { _id: 'a2', name: 'y', isDeployed: false },
    ]);
    expect(list).toHaveBeenCalledTimes(1);
    expect(one).not.toHaveBeenCalled();
  });

  it('shows them as not deployed when the list fails', async () => {
    vi.spyOn(agentService, 'getDeployments').mockRejectedValue(new Error('down'));
    expect(await agentService.withDeploymentStatus([{ _id: 'a1' }])).toEqual([{ _id: 'a1', isDeployed: false }]);
  });
});

describe('agentService.getAgents', () => {
  it('asks for names, not every agent\'s code', async () => {
    vi.spyOn(agentService, '_getAuthHeaders').mockResolvedValue({});
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => [] });
    await agentService.getAgents();
    expect(new URL(fetchMock.mock.calls[0][0], 'http://x').searchParams.get('summary')).toBe('true');
  });
});
