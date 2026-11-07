// Formatting the memory screens share.

export const TIER_TEXT = {
  raw: 'raw, not yet reviewed',
  curated: 'curated',
  verified: 'verified',
};

export function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}

export function today() {
  return new Date().toISOString().slice(0, 10);
}

export function isOverdue(hit) {
  return Boolean(hit.review_by) && hit.review_by <= today();
}

export function plusDays(n) {
  return new Date(Date.now() + n * 86400000).toISOString().slice(0, 10);
}

// What each decision does, by the entry's tier now.
export function choicesFor(tier) {
  const keep = {
    action: 'keep', disc: tier, verb: 'Keep',
    label: `Keep ${tier}`, help: 'still holds; reset the review date',
  };
  const curate = { action: 'curate', disc: 'curated', verb: 'Curate', label: 'Curate', help: 'read and summarised; fit to cite' };
  const verify = { action: 'verify', disc: 'verified', verb: 'Verify', label: 'Verify', help: 'checked against the source, safe to act on' };
  const demote = { action: 'demote', disc: null, verb: 'Demote', label: 'Demote', help: 'wrong or no longer true; kept in history, out of search' };
  if (tier === 'raw') return [curate, verify, keep, demote];
  if (tier === 'curated') return [keep, verify, demote];
  return [keep, demote];
}
