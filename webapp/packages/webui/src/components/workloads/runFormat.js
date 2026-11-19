// Formatting the Workloads and run pages share.
import { parseServerDate } from '../../utils/serverTime';

export function duration(startIso, endIso) {
  const a = parseServerDate(startIso);
  const b = endIso ? parseServerDate(endIso) : new Date();
  if (!a || !b) return '';
  const mins = Math.max(0, Math.round((b - a) / 60000));
  if (mins < 60) return `${mins} m`;
  return `${Math.floor(mins / 60)} h ${mins % 60} m`;
}

// A run waiting for a slot (RUN_MAX_CONCURRENT / RUN_MAX_PER_USER) is as
// live as a running one: it starts by itself.
export const LIVE_STATUSES = ['running', 'queued'];

// "Queued — 3 ahead of you" from a run summary's queueAhead.
export function queuedText(run) {
  const n = run?.queueAhead;
  if (n === 0) return 'Queued — next in line';
  if (typeof n === 'number') return `Queued — ${n} ahead of you`;
  return 'Queued';
}

export function when(iso) {
  const d = parseServerDate(iso);
  if (!d) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (sameDay) return `Today, ${time}`;
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}, ${time}`;
}

// "tooling-llmao @ 9445a24" from a run's inputs, for ASVS-shaped workloads,
// else the first input value.
export function runTarget(inputs) {
  if (!inputs) return '';
  const repo = inputs.sourceRepo || inputs.repository || inputs.repo;
  const commit = inputs.commit;
  if (repo) {
    const short = String(repo).replace(/^apache\//, '');
    return commit ? `${short} @ ${String(commit).slice(0, 12)}` : short;
  }
  const first = Object.values(inputs).find((v) => typeof v === 'string' && !v.startsWith('('));
  return first || '';
}

export const runPath = (runId) => `/run/${encodeURIComponent(runId)}`;

export const memoriesPath = (domain, runId, select, tab) => {
  const p = new URLSearchParams({ run: runId });
  if (select) p.set('select', select);
  if (tab) p.set('tab', tab);
  return `/memories/${encodeURIComponent(domain)}?${p}`;
};
