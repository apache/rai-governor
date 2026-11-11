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
    if (data && data.detail) {
      if (typeof data.detail === 'string') detail = data.detail;
      // FastAPI's validation errors: say which field and what's wrong.
      else if (Array.isArray(data.detail)) {
        detail = data.detail.map((d) => {
          const where = (d.loc || []).filter((x) => x !== 'body').join(' › ');
          return where ? `${where}: ${(d.msg || '').replace(/^Value error, /, '')}` : (d.msg || '');
        }).join('; ');
      } else detail = JSON.stringify(data.detail);
    }
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

  // A project's PMC puts a published workload on the project's page, with
  // the project's defaults, changes those, or takes it off.
  add(workloadId, project, defaults = {}) {
    return request(`/workloads/${enc(workloadId)}/add`, {
      method: 'POST',
      body: JSON.stringify({ project, defaults }),
    });
  },

  setDefaults(workloadId, project, defaults = {}) {
    return request(`/workloads/${enc(workloadId)}/add`, {
      method: 'PUT',
      body: JSON.stringify({ project, defaults }),
    });
  },

  remove(workloadId, project) {
    return request(`/workloads/${enc(workloadId)}/add?project=${enc(project)}`, { method: 'DELETE' });
  },

  // The catalog, for site admins: whole workloads, fixed inputs included.
  catalog() {
    return request('/workloads/admin');
  },

  publish(body) {
    return request('/workloads/admin', { method: 'POST', body: JSON.stringify(body) });
  },

  update(workloadId, body) {
    return request(`/workloads/admin/${enc(workloadId)}`, { method: 'PUT', body: JSON.stringify(body) });
  },

  unpublish(workloadId) {
    return request(`/workloads/admin/${enc(workloadId)}`, { method: 'DELETE' });
  },

  deployments() {
    return request('/deployments');
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
