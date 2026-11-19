// A delete the API refuses (the agent is still called by others), or an
// export it can't make, says so by the action buttons, where the person
// clicked.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, fireEvent, render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import ViewAgent from './ViewAgent';
import agentService from '../services/agentService';
import pipelineService from '../services/pipelineService';
import { AgentCreationFlowContext } from './AgentCreationFlow/AgentCreationFlowContextValue';

const REFUSAL = 'This agent is called by demo_main (main). Remove it from those agents before deleting it.';

vi.mock('../services/agentService', () => ({
  default: {
    getAgent: vi.fn(() => Promise.resolve({
      _id: 'step', name: 'demo_step', description: 'd', code: 'async def run(i, t):\n    return {}\n',
      tools: {}, swaggerSpecs: [], gofannonAgents: [], inputSchema: {}, outputSchema: {},
      invokableModels: [],
    })),
    getAgents: vi.fn(() => Promise.resolve([])),
    getDeployment: vi.fn(() => Promise.resolve({ is_deployed: false })),
    deleteAgent: vi.fn(),
  },
}));
vi.mock('../services/chatService', () => ({ default: { getProviders: vi.fn(() => Promise.resolve({})) } }));
vi.mock('../services/pipelineService', () => ({ default: { downloadExport: vi.fn() } }));
vi.mock('../components/AgentChainView', () => ({ default: () => null }));

function renderPage() {
  return render(
    <AgentCreationFlowContext.Provider value={{}}>
      <MemoryRouter initialEntries={['/agent/step']}>
        <Routes><Route path="/agent/:agentId" element={<ViewAgent />} /></Routes>
      </MemoryRouter>
    </AgentCreationFlowContext.Provider>,
  );
}

describe('ViewAgent delete', () => {
  afterEach(() => { vi.clearAllMocks(); sessionStorage.clear(); });

  it('shows the refusal by the action buttons', async () => {
    agentService.deleteAgent.mockRejectedValue(new Error(REFUSAL));
    renderPage();
    // Text queries: role queries over this page are slow enough to time out.
    fireEvent.click(await screen.findByText('Delete Agent'));
    fireEvent.click(screen.getByText('Delete', { selector: 'button' }));
    const msg = await screen.findByText(`Delete failed: ${REFUSAL}`);
    expect(agentService.deleteAgent).toHaveBeenCalledWith('step');
    const alert = msg.closest('[role="alert"]');
    expect(alert.nextElementSibling).toBe(document.querySelector('[aria-label="Agent actions"]'));
  });

  it('shows why an export failed by the action buttons', async () => {
    const why = 'agent gone not found: demo_step (step) calls it';
    pipelineService.downloadExport.mockRejectedValue(new Error(why));
    renderPage();
    fireEvent.click(await screen.findByText('Export pipeline'));
    const msg = await screen.findByText(`Export failed: ${why}`);
    expect(msg.closest('[role="alert"]').nextElementSibling)
      .toBe(document.querySelector('[aria-label="Agent actions"]'));
  });
});
