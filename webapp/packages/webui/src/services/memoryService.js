// Memory pilot client (ISSUE-026). See the API's routes_memory.py.
//
// The API mounts /memory only when MEMORY_ENABLED is true, so a 404 from
// /memory/status means the page and its nav link should not show.
import config from '../config';

const API_BASE_URL = config.api.baseUrl;

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
  });
  if (response.ok) return response.json();
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

const enc = encodeURIComponent;

function dispositionBody({ kind, why, duplicateOf, filedAs, guidance }) {
  const body = { kind, why: why || '' };
  if (duplicateOf) body.duplicate_of = duplicateOf;
  if (filedAs) body.filed_as = filedAs;
  if (guidance) body.guidance = guidance;
  return body;
}

// One /memory/status call per page load decides whether memory is on.
// Not /memory/domains: that counts every domain's entries, and every page
// asks this for its nav link.
let enabledOnce = null;

const memoryService = {
  domains() {
    return request('/memory/domains');
  },

  // Resolves true or false, never throws.
  isEnabled() {
    if (!enabledOnce) {
      enabledOnce = request('/memory/status').then(() => true, () => false);
    }
    return enabledOnce;
  },

  _resetEnabled() {
    enabledOnce = null;
  },

  search(domain, { q, type, trustTier, dueBy, includeInactive, order, limit, run, queue, origins, disposition } = {}) {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    (origins || []).forEach((o) => params.append('origin', o));
    if (disposition) params.set('disposition', disposition);
    if (run) params.set('run', run);
    if (type) params.set('type', type);
    if (trustTier) params.set('trust_tier', trustTier);
    if (dueBy) params.set('due_by', dueBy);
    if (includeInactive) params.set('include_inactive', 'true');
    if (order) params.set('order', order);
    if (queue) params.set('queue', 'true');
    params.set('limit', String(limit || 50));
    return request(`/memory/${enc(domain)}/search?${params}`);
  },

  get(id) {
    return request(`/memory/entries/${enc(id)}`);
  },

  history(id) {
    return request(`/memory/entries/${enc(id)}/history`);
  },

  version(id, version) {
    return request(`/memory/entries/${enc(id)}/versions/${enc(version)}`);
  },

  recall(domain, question) {
    return request(`/memory/${enc(domain)}/recall`, {
      method: 'POST',
      body: JSON.stringify({ question }),
    });
  },

  // The runs that wrote into a domain, newest first, with their entries
  // counted by type and tier (B11).
  runs(domain, limit = 200) {
    return request(`/memory/${enc(domain)}/runs?limit=${limit}`);
  },

  // One run's entries tallied per readable domain, and what the caller may
  // know about the run.
  forRun(runId) {
    return request(`/memory/by-run/${enc(runId)}`);
  },

  batchReview({ ids, action, why, reviewBy, restoreSuperseded }) {
    const body = { ids, action, why: why || '', restore_superseded: !!restoreSuperseded };
    if (reviewBy) body.review_by = reviewBy;
    return request('/memory/reviews/batch', { method: 'POST', body: JSON.stringify(body) });
  },

  undoBatch(batch) {
    return request(`/memory/reviews/batch/${enc(batch)}/undo`, { method: 'POST' });
  },

  // Triage (scans that remember). A disposition is { kind, why,
  // duplicateOf, filedAs, guidance: { text, summary, scope, always } }.
  dispose(id, d) {
    return request(`/memory/entries/${enc(id)}/disposition`, { method: 'POST', body: JSON.stringify(dispositionBody(d)) });
  },

  disposeBatch(ids, d) {
    return request('/memory/dispositions/batch', { method: 'POST', body: JSON.stringify({ ids, ...dispositionBody(d) }) });
  },

  acceptProposals(ids, { why, edits } = {}) {
    return request('/memory/proposals/accept', { method: 'POST', body: JSON.stringify({ ids, why: why || '', edits: edits || {} }) });
  },

  rejectProposals(ids, why) {
    return request('/memory/proposals/reject', { method: 'POST', body: JSON.stringify({ ids, why }) });
  },

  guidanceRepos(domain) {
    return request(`/memory/${enc(domain)}/guidance-repos`);
  },

  guidance(domain, repo, scanner) {
    const params = new URLSearchParams({ repo });
    if (scanner) params.set('scanner', scanner);
    return request(`/memory/${enc(domain)}/guidance?${params}`);
  },

  digest(domain, repo) {
    return request(`/memory/${enc(domain)}/digest?${new URLSearchParams({ repo })}`);
  },

  acceptDigest(id, why) {
    return request(`/memory/digests/${enc(id)}/accept`, { method: 'POST', body: JSON.stringify({ why: why || '' }) });
  },

  review(id, { action, why, reviewBy }) {
    const body = { action, why: why || '' };
    if (reviewBy) body.review_by = reviewBy;
    return request(`/memory/entries/${enc(id)}/review`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },
};

export default memoryService;
