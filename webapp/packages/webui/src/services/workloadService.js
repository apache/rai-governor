// Workloads (B11): what a project's members can run, and the runs they made.
// See the API's routes_workloads.py. The page lives under /projects/ and a
// run under /run/: the API owns /workloads and /runs.
import config from '../config';

const API_BASE_URL = (config?.api?.baseUrl || '').replace(/\/$/, '');
const enc = encodeURIComponent;

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  if (response.ok) return response.status === 204 ? null : response.json();
  let detail = `Request failed: ${response.status}`;
  try {
    const data = await response.json();
    if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
  } catch {
    // Not JSON: keep the status message.
  }
  const error = new Error(detail);
  error.status = response.status;
  throw error;
}

const workloadService = {
  projects() {
    return request('/workloads/projects');
  },

  list(project) {
    return request(`/workloads?project=${enc(project)}`);
  },

  run(workloadId, project, inputs) {
    return request(`/workloads/${enc(workloadId)}/run`, {
      method: 'POST',
      body: JSON.stringify({ project, inputs }),
    });
  },

  runs(project, { workloadId, limit } = {}) {
    const p = new URLSearchParams({ project });
    if (workloadId) p.set('workload_id', workloadId);
    if (limit) p.set('limit', String(limit));
    return request(`/runs?${p}`);
  },

  getRun(runId) {
    return request(`/runs/${enc(runId)}`);
  },

  stopRun(runId) {
    return request(`/runs/${enc(runId)}/stop`, { method: 'POST' });
  },
};

export default workloadService;
