// Pipeline import and export (ISSUE-011). See the API's routes_pipelines.py.
//
// Imports always preview first: the same call with dryRun true returns the
// plan (create / update / unchanged per agent) without writing anything.
import config from '../config';

const API_BASE_URL = config.api.baseUrl;

async function asJson(response, fallback) {
  if (response.ok) return response.json();
  let detail = fallback;
  try {
    const data = await response.json();
    if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
  } catch {
    // Not JSON: keep the fallback message.
  }
  throw new Error(detail);
}

// Where an imported copy came from, for its label chip's tooltip.
export function sourceText(source) {
  if (!source) return '';
  if (source.kind === 'github') {
    return `${source.repo}${source.path ? `/${source.path}` : ''} @ ${(source.commit || source.ref || '').slice(0, 7)}`;
  }
  if (source.kind === 'upload') return 'folder upload';
  return source.kind || '';
}

const pipelineService = {
  // deploy (default true): deploy every agent the import writes.
  // labeled (default true): false imports into the stable copy, at the plain
  // agent names and /rest/<name>, and ignores label.
  async importFromGithub({ repo, ref, path, token, label, dryRun, deploy = true, labeled = true }) {
    const body = { repo, ref: ref || 'main', path: path || '', dryRun, deploy, labeled };
    if (token) body.token = token;
    if (label) body.label = label;
    const response = await fetch(`${API_BASE_URL}/pipelines/import/github`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    return asJson(response, 'GitHub import failed.');
  },

  // files: the FileList from <input webkitdirectory>. Each File carries the
  // path inside the picked folder as webkitRelativePath.
  async importFromFolder({ files, label, dryRun, deploy = true, labeled = true }) {
    const form = new FormData();
    Array.from(files).forEach((f) => {
      form.append('files', f, f.webkitRelativePath || f.name);
    });
    if (label) form.append('label', label);
    form.append('dryRun', dryRun ? 'true' : 'false');
    form.append('deploy', deploy ? 'true' : 'false');
    form.append('labeled', labeled ? 'true' : 'false');
    const response = await fetch(`${API_BASE_URL}/pipelines/import/upload`, {
      method: 'POST',
      body: form,
    });
    return asJson(response, 'Folder import failed.');
  },

  // Downloads <pipeline>.zip: the agent and every agent it calls.
  async downloadExport(agentId) {
    const response = await fetch(`${API_BASE_URL}/agents/${agentId}/export`);
    if (!response.ok) await asJson(response, 'Export failed.');
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : 'pipeline.zip';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return filename;
  },
};

export default pipelineService;
