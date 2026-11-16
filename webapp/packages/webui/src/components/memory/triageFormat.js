// What the Triage tab shows about a finding, from its index row: the
// disposition a person gave it, and the evidence a scan put there
// (memory.annotate). Evidence shape, all optional:
//   finding:   { global_id, severity, file, commit }
//   matches:   [{ relation, ref, title, url, state, state_reason, confidence, why, decision }]
//                relation: tracks | addresses | fixed | declined | same_finding | decided_before
//   patch:     { status, reason }
//   suggested: { kind, confidence, why, guidance, duplicate_of, scope }

export const KINDS = [
  { kind: 'valid', label: 'Valid', hint: 'worth fixing' },
  { kind: 'duplicate', label: 'Duplicate', hint: 'of a finding, issue or PR' },
  { kind: 'fp_wrong', label: 'False positive', hint: 'the finding is wrong' },
  { kind: 'fp_context', label: 'False positive', hint: 'the scanner lacked context · writes guidance' },
  { kind: 'by_design', label: 'By design', hint: 'or accepted risk · writes guidance as a decision' },
];

export const WRITES_GUIDANCE = ['fp_context', 'by_design'];

export function kindLabel(kind) {
  const k = KINDS.find((x) => x.kind === kind);
  if (!k) return kind;
  if (kind === 'fp_wrong') return 'false positive, the finding is wrong';
  if (kind === 'fp_context') return 'false positive, the scanner lacked context';
  return k.label.toLowerCase();
}

export const isFinding = (h) => /^(asvs|glasswing):finding:/.test(h.origin || '');
export const isProposal = (h) => (h.origin || '').startsWith('proposal:guidance:');

// owner/repo from a finding's or a proposal's origin.
export function repoOf(h) {
  const m = /^(?:asvs|glasswing):finding:(.+)$/.exec(h.origin || '') || /^proposal:guidance:(.+)$/.exec(h.origin || '');
  if (!m) return null;
  const parts = m[1].split('/');
  return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : null;
}

export const scannerOf = (h) => (/^(asvs|glasswing):/.exec(h.origin || '') || [])[1] || null;

const PATCH = {
  proposed_accepted: 'patch accepted',
  proposed_concerns: 'patch with concerns',
  proposed_rejected: 'patch rejected',
  proposed_unreviewed: 'patch not reviewed',
  false_positive: 'disputed',
  needs_design: 'needs design',
  unlocated: 'files not found',
  failed: 'drafting failed',
  skipped_open_pr: 'skipped',
  skipped_not_planned: 'skipped',
  skipped_informational: 'not patched',
  skipped_decided: 'skipped, decided before',
};

export const patchText = (p) => (p?.status ? PATCH[p.status] || p.status.replace(/_/g, ' ') : null);

// One badge per match, worded as the mockups do.
export function matchText(m) {
  switch (m.relation) {
    case 'tracks': return `${m.ref} open`;
    case 'addresses': return `PR ${m.ref} open`;
    case 'fixed': return `${m.ref} closed, fixed`;
    case 'declined': return `${m.ref} closed, not planned`;
    case 'same_finding': return `Same as ${m.ref}`;
    case 'decided_before': {
      const d = m.decision || {};
      return `Dismissed before: ${kindLabel(d.kind) || 'decided'}${d.by ? `, ${d.by.replace(/^human:/, '')}` : ''}`;
    }
    default: return m.ref || m.relation;
  }
}

export function category(h) {
  if (isProposal(h)) return h.status ? 'closed' : 'proposals';
  if (!isFinding(h)) return 'other';
  if (h.disposition) return 'decided';
  if (h.status) return 'closed';
  return h.evidence?.suggested?.kind ? 'suggested' : 'needs';
}

export const unlocated = (h) => h.evidence?.patch?.status === 'unlocated';
