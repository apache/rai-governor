// Turning a deployed agent's input schema into a workload's inputs, and back.
// Shared by the publish form and its tests.

const SECRET_RE = /token|secret|password|passwd|key|pat\b|credential|auth/i;

export const isSecretName = (name) => SECRET_RE.test(name);

export function humanize(name) {
  const words = String(name || '').replace(/[_-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim();
  return words ? words[0].toUpperCase() + words.slice(1).toLowerCase() : '';
}

// A first guess at each input's role, for a new workload: secrets and
// where results go are the admin's; a source repository is the member's
// pick from the project's; the rest the admin decides.
export function guessRow(name) {
  const base = {
    name, label: humanize(name), kind: 'text', default: '', choicesText: '', pattern: '', help: '',
    required: false, value: '',
  };
  if (isSecretName(name) || /^output/i.test(name)) return { ...base, role: 'fixed' };
  if (/repo/i.test(name)) return { ...base, role: 'member', kind: 'repository', label: 'Repository', required: true };
  if (/^(commit|branch|ref)$/i.test(name)) {
    return { ...base, role: 'member', help: 'Blank: the latest on the default branch' };
  }
  return { ...base, role: 'unused' };
}

// Rows for the form: from a workload being edited, then any other inputs
// the deployed agent takes.
export function rowsFor(schemaKeys, workload) {
  const rows = [];
  const seen = new Set();
  for (const i of workload?.inputs || []) {
    rows.push({
      name: i.name, role: 'member', label: i.label, kind: i.kind || 'text', default: i.default ?? '',
      choicesText: (i.choices || []).join(', '), pattern: i.pattern ?? '', help: i.help ?? '',
      required: i.required !== false, value: '',
    });
    seen.add(i.name);
  }
  for (const [k, v] of Object.entries(workload?.fixedInputs || {})) {
    // A value that isn't a string (a number, a list, an object) is edited as
    // JSON and sent back as the same type: the agent gets 5, not "5".
    const json = typeof v !== 'string';
    rows.push({ ...guessRow(k), role: 'fixed', json, value: json ? JSON.stringify(v) : v });
    seen.add(k);
  }
  for (const k of schemaKeys) {
    if (!seen.has(k)) rows.push(workload ? { ...guessRow(k), role: 'unused' } : guessRow(k));
  }
  return rows;
}

const REPO_LINE = /^\s*([A-Za-z0-9][A-Za-z0-9-]*)\s*:\s*(.*)$/;

export function parseRepositories(text) {
  const out = {};
  for (const line of String(text || '').split('\n')) {
    const m = line.match(REPO_LINE);
    if (!m) continue;
    const repos = m[2].split(/[,\s]+/).map((r) => r.trim()).filter(Boolean);
    if (repos.length) out[m[1].toLowerCase()] = repos;
  }
  return out;
}

// Lines that say nothing parseRepositories can use: refused rather than
// dropped, or that project would quietly get the looser naming rule.
export function badRepositoryLines(text) {
  return String(text || '').split('\n').map((l) => l.trim())
    .filter((l) => l && !(REPO_LINE.test(l) && l.replace(REPO_LINE, '$2').trim()));
}

export function repositoriesText(repos) {
  return Object.entries(repos || {}).map(([p, list]) => `${p}: ${list.join(', ')}`).join('\n');
}

export function bodyFrom(form) {
  const inputs = form.rows.filter((r) => r.role === 'member').map((r) => {
    const i = { name: r.name, label: r.label || humanize(r.name), kind: r.kind, required: Boolean(r.required) };
    if (r.default) i.default = r.default;
    if (r.help) i.help = r.help;
    if (r.kind === 'choice') i.choices = r.choicesText.split(',').map((c) => c.trim()).filter(Boolean);
    if (r.kind === 'text' && r.pattern) i.pattern = r.pattern;
    return i;
  });
  const fixedInputs = Object.fromEntries(form.rows.filter((r) => r.role === 'fixed').map((r) => {
    if (!r.json) return [r.name, r.value];
    try {
      return [r.name, JSON.parse(r.value)];
    } catch {
      throw new Error(`${r.name}: not valid JSON (it was stored as ${/^[[{]/.test(r.value.trim()) ? 'a list or object' : 'a number or true/false'})`);
    }
  }));
  const bad = badRepositoryLines(form.reposText);
  if (bad.length) throw new Error(`Repositories: can't read ${bad.map((l) => `"${l}"`).join(', ')}. Write "project: apache/repo, apache/repo".`);
  const projects = form.scope === 'all'
    ? ['*']
    : form.projectsText.split(/[,\s]+/).map((p) => p.trim().toLowerCase()).filter(Boolean);
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    deployment: form.deployment,
    projects,
    inputs,
    fixedInputs,
    repositories: parseRepositories(form.reposText),
    produces: form.produces.trim() || null,
    memoryNote: (form.memoryNote || '').trim() || null,
    typicalDuration: form.typicalDuration.trim() || null,
    enabled: Boolean(form.enabled),
  };
}
