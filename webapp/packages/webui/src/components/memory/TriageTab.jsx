// Triage: a person decides on each finding a scan run wrote (scans that
// remember). Each decision is one of five dispositions; "needs context"
// and "by design" also write guidance every later scan of the repo is
// given. Proposals from the pipeline wait here for Accept or Reject, and
// the repo's guidance digest for a person to accept each new version.
//
// A suggestion only fills the form in: nothing is decided until it is
// recorded. Keys, with the list focused: j and k move, x ticks.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  FormControlLabel,
  Paper,
  Radio,
  RadioGroup,
  Stack,
  TextField,
  Typography,
} from '@mui/material';
import memoryService from '../../services/memoryService';
import { EntryBody } from './memoryBits';
import {
  KINDS, WRITES_GUIDANCE, category, isFinding, isProposal, kindLabel, matchText, patchText,
  repoOf, unlocated,
} from './triageFormat';

const FILTERS = [
  { key: 'needs', label: 'Needs a decision' },
  { key: 'suggested', label: 'Has a suggestion' },
  { key: 'proposals', label: 'Proposals' },
  { key: 'decided', label: 'Decided' },
  { key: 'unlocated', label: 'Files not found' },
];

const SEV_COLOR = { Critical: '#7f1d1d', High: '#b91c1c', Medium: '#b45309', Low: '#3f3f46' };

const badgeSx = { height: 20, fontSize: '0.7rem', bgcolor: '#f4f4f5', border: '1px solid #e4e4e7' };
const suggestSx = { height: 20, fontSize: '0.7rem', bgcolor: '#eff6ff', color: '#1e40af', border: '1px solid #bfdbfe' };
const decidedSx = { height: 20, fontSize: '0.7rem', bgcolor: '#ecfdf5', color: '#065f46', border: '1px solid #a7f3d0' };
const darkBtn = { bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } };

function matchesIn(hits, key) {
  return hits.filter((h) => {
    const c = category(h);
    if (key === 'needs') return c === 'needs' || c === 'suggested';
    if (key === 'unlocated') return (c === 'needs' || c === 'suggested') && unlocated(h);
    return c === key;
  });
}

function Badges({ hit }) {
  const ev = hit.evidence || {};
  const patch = patchText(ev.patch);
  return (
    <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', mt: 0.75 }}>
      {(ev.matches || []).map((m, i) => <Chip key={i} label={matchText(m)} size="small" sx={badgeSx} />)}
      {patch && <Chip label={`Patch: ${patch}`} size="small" sx={badgeSx} />}
      {ev.seen_again?.length > 0 && (
        <Chip label={`Found again by ${ev.seen_again.length} later ${ev.seen_again.length === 1 ? 'scan' : 'scans'}`} size="small" sx={badgeSx} />
      )}
      {hit.disposition && <Chip label={`Decided: ${kindLabel(hit.disposition)}`} size="small" sx={decidedSx} />}
      {!hit.disposition && ev.suggested?.kind && (
        <Chip label={`Suggested: ${kindLabel(ev.suggested.kind)}${ev.suggested.duplicate_of ? ` of ${ev.suggested.duplicate_of}` : ''}`}
          size="small" sx={suggestSx} />
      )}
    </Box>
  );
}

function FindingRow({ hit, selected, onClick, checked, onCheck }) {
  const f = hit.evidence?.finding || {};
  return (
    <Paper
      variant="outlined" onClick={onClick}
      sx={{
        p: 1.5, cursor: 'pointer', display: 'flex', gap: 1, alignItems: 'flex-start',
        borderColor: selected ? '#18181b' : undefined, borderWidth: selected ? 1.5 : 1,
        '&:hover': { borderColor: '#71717a' },
      }}
    >
      {onCheck && (
        <Checkbox
          size="small" checked={!!checked} disabled={Boolean(hit.status)}
          onClick={(e) => e.stopPropagation()} onChange={(e) => onCheck(e.target.checked)}
          slotProps={{ input: { 'aria-label': `Select: ${hit.title || hit.id}` } }}
          sx={{ p: 0.25, mt: -0.25 }}
        />
      )}
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {f.severity && <span style={{ color: SEV_COLOR[f.severity] || '#3f3f46', fontWeight: 600 }}>{f.severity} </span>}
          {f.global_id}{f.file && <> · <code>{f.file}</code></>}
        </Typography>
        <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.25 }}>{hit.title || hit.id}</Typography>
        <Badges hit={hit} />
      </Box>
    </Paper>
  );
}

// The guidance part of the form: the text later scans are given, its
// scope, and Always use.
function GuidanceFields({ g, setG, count }) {
  return (
    <Box sx={{ pl: 4, display: 'flex', flexDirection: 'column', gap: 1 }}>
      <TextField
        multiline minRows={3} size="small" label={`Guidance for later scans${count > 1 ? `, one entry citing all ${count}` : ''}`}
        value={g.text} onChange={(e) => setG({ ...g, text: e.target.value })}
        helperText="A draft is offered where the pipeline has one; edit freely."
      />
      <RadioGroup row value={g.scope} onChange={(e) => setG({ ...g, scope: e.target.value })} aria-label="Guidance scope">
        <FormControlLabel value="all" control={<Radio size="small" />} label={<Typography variant="body2">This repo, all scanners</Typography>} />
        <FormControlLabel value="asvs" control={<Radio size="small" />} label={<Typography variant="body2">ASVS only</Typography>} />
        <FormControlLabel value="glasswing" control={<Radio size="small" />} label={<Typography variant="body2">Glasswing only</Typography>} />
      </RadioGroup>
      <FormControlLabel
        control={<Checkbox size="small" checked={g.always} onChange={(e) => setG({ ...g, always: e.target.checked })} />}
        label={<Typography variant="body2">Always use: load it even when the prompt budget is tight</Typography>}
      />
    </Box>
  );
}

function DispositionForm({ hits, suggestion, onDone, batch }) {
  const one = hits[0];
  const [kind, setKind] = useState(suggestion?.kind || '');
  const [why, setWhy] = useState(suggestion?.why || '');
  const [dup, setDup] = useState(suggestion?.duplicate_of || '');
  const [filed, setFiled] = useState('');
  const [g, setG] = useState({ text: suggestion?.guidance || '', scope: suggestion?.scope || 'all', always: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const needsGuidance = WRITES_GUIDANCE.includes(kind);
  const ready = kind && why.trim() && (!needsGuidance || g.text.trim()) && (kind !== 'duplicate' || dup.trim());

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const d = { kind, why, duplicateOf: kind === 'duplicate' ? dup : undefined, filedAs: kind === 'valid' ? filed : undefined,
      guidance: needsGuidance ? { text: g.text, scope: g.scope, always: g.always } : undefined };
    try {
      const out = batch ? await memoryService.disposeBatch(hits.map((h) => h.id), d) : await memoryService.dispose(one.id, d);
      onDone(out);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box component="form" onSubmit={submit} aria-label={batch ? `Decide on ${hits.length} findings` : 'Decide on this finding'}
      sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Typography variant="subtitle2">{batch ? `One disposition for all ${hits.length}` : 'Disposition'}</Typography>
      <RadioGroup value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Disposition">
        {KINDS.map((k) => (
          <Box key={k.kind}>
            <FormControlLabel
              value={k.kind} control={<Radio size="small" />}
              label={<Typography variant="body2"><b>{k.label}</b>: {k.hint}</Typography>}
            />
            {kind === k.kind && k.kind === 'duplicate' && (
              <TextField size="small" label="Duplicate of (finding, issue or PR)" value={dup}
                onChange={(e) => setDup(e.target.value)} sx={{ ml: 4, maxWidth: 420 }} />
            )}
            {kind === k.kind && k.kind === 'valid' && (
              <TextField size="small" label="Filed as (optional): private issue or advisory" value={filed}
                onChange={(e) => setFiled(e.target.value)} sx={{ ml: 4, maxWidth: 420 }} />
            )}
            {kind === k.kind && WRITES_GUIDANCE.includes(k.kind) && (
              <GuidanceFields g={g} setG={setG} count={hits.length} />
            )}
          </Box>
        ))}
      </RadioGroup>
      <TextField size="small" label="Why (recorded with the decision)" value={why} onChange={(e) => setWhy(e.target.value)} />
      {suggestion?.kind && (
        <Typography variant="caption" color="text.secondary">
          A suggestion is only a starting point: nothing is decided until you record it.
        </Typography>
      )}
      <Box>
        <Button type="submit" variant="contained" disabled={busy || !ready} sx={darkBtn}>
          {busy ? <CircularProgress size={16} color="inherit" />
            : `Record${batch ? ` for ${hits.length}` : ''}${needsGuidance ? ' + guidance' : ''}`}
        </Button>
      </Box>
      {error && <Alert severity="error">{error}</Alert>}
    </Box>
  );
}

function Result({ result, onUndone }) {
  const [undo, setUndo] = useState(null);
  const [busy, setBusy] = useState(false);
  if (undo) {
    return (
      <Alert severity={undo.error ? 'error' : 'info'} sx={{ mb: 2 }}>
        {undo.error || `Undid ${undo.undone.length}${undo.withdrawn?.length ? ` and withdrew ${undo.withdrawn.length} guidance ${undo.withdrawn.length === 1 ? 'entry' : 'entries'}` : ''}.`}
        {undo.skipped?.length > 0 && ` ${undo.skipped.length} left as they are: changed since.`}
      </Alert>
    );
  }
  const n = result.applied?.length ?? 0;
  return (
    <Alert
      severity={n ? 'success' : 'warning'} role="status" sx={{ mb: 2 }}
      action={n > 0 && result.batch?.startsWith('d-') && (
        <Button color="inherit" size="small" disabled={busy} onClick={async () => {
          setBusy(true);
          try { setUndo(await memoryService.undoBatch(result.batch)); onUndone?.(); } catch (e) { setUndo({ error: e.message }); }
          setBusy(false);
        }}>Undo</Button>
      )}
    >
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {result.message || `Recorded ${kindLabel(result.kind)} for ${n} ${n === 1 ? 'finding' : 'findings'}`}
        {result.guidance?.length > 0 && `, and wrote ${result.guidance.length} guidance ${result.guidance.length === 1 ? 'entry' : 'entries'}`}
      </Typography>
      {result.skipped?.length > 0 && (
        <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, fontSize: 13 }}>
          {result.skipped.map((s) => <li key={s.id}><code>{s.id}</code>: {s.reason}</li>)}
        </Box>
      )}
    </Alert>
  );
}

function FindingDetail({ hit, canReview, onDone }) {
  const [entry, setEntry] = useState(null);
  useEffect(() => {
    let live = true;
    setEntry(null);
    memoryService.get(hit.id).then((e) => { if (live) setEntry(e); }, () => { if (live) setEntry({ body: '' }); });
    return () => { live = false; };
  }, [hit.id]);
  const ev = hit.evidence || {};
  const f = ev.finding || {};
  const d = hit.decision;
  return (
    <Paper variant="outlined" sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Typography variant="caption" color="text.secondary">
        <code>{hit.id}</code> · {f.global_id || 'finding'} · {hit.trust_tier}{hit.status ? ` · ${hit.status}` : ''}
      </Typography>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>{hit.title || hit.id}</Typography>
      {(f.severity || f.file) && (
        <Typography variant="body2" color="text.secondary">
          {f.severity}{f.file && <> · <code>{f.file}</code>{f.commit ? ` @ ${f.commit}` : ''}</>}
        </Typography>
      )}
      {(ev.matches?.length > 0 || ev.patch) && (
        <Box>
          <Typography variant="overline" sx={{ fontSize: '0.65rem', color: 'text.secondary' }}>What the pipeline found</Typography>
          {(ev.matches || []).map((m, i) => (
            <Typography key={i} variant="body2" sx={{ mb: 0.5 }}>
              {m.url ? <a href={m.url} target="_blank" rel="noreferrer" style={{ color: 'inherit' }}>{matchText(m)}</a> : matchText(m)}
              {m.title && <> · <i>{m.title}</i></>}
              {(m.confidence || m.why) && <span style={{ color: '#71717a' }}> · {m.confidence ? `${m.confidence} confidence` : ''}{m.why ? `${m.confidence ? ', ' : ''}${m.why}` : ''}</span>}
            </Typography>
          ))}
          {ev.patch && (
            <Typography variant="body2"><b>Patch: {patchText(ev.patch)}</b>{ev.patch.reason && ` · ${ev.patch.reason}`}</Typography>
          )}
        </Box>
      )}
      {entry === null ? <CircularProgress size={18} /> : (
        <Box sx={{ maxHeight: 220, overflow: 'auto', borderTop: '1px solid #f4f4f5', pt: 1 }}><EntryBody body={entry.body} /></Box>
      )}
      {d ? (
        <Alert severity="success" icon={false}>
          <b>Decided: {kindLabel(d.kind)}</b> by <code>{d.by}</code> · {d.why}
          {d.duplicate_of && <> · duplicate of {d.duplicate_of}</>}
          {d.filed_as && <> · filed as {d.filed_as}</>}
          {d.guidance_id && <> · guidance <RouterLink to={`/memories/entries/${encodeURIComponent(d.guidance_id)}`}>{d.guidance_id}</RouterLink></>}
        </Alert>
      ) : canReview && !hit.status ? (
        <DispositionForm key={hit.id} hits={[hit]} suggestion={ev.suggested} onDone={onDone} />
      ) : null}
    </Paper>
  );
}

function ProposalCard({ hit, checked, onCheck, onDone, canReview }) {
  const [entry, setEntry] = useState(null);
  const [mode, setMode] = useState(null);       // null | edit | reject
  const [text, setText] = useState('');
  const [scope, setScope] = useState('all');
  const [always, setAlways] = useState(false);
  const [why, setWhy] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    memoryService.get(hit.id).then((e) => { if (live) { setEntry(e); setText(e.body.trim()); } }, () => {});
    return () => { live = false; };
  }, [hit.id]);

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try { onDone(await fn()); } catch (e) { setError(e.message); }
    setBusy(false);
  };
  const accept = () => act(async () => {
    const edits = mode === 'edit' ? { [hit.id]: { text, scope, always } } : {};
    const out = await memoryService.acceptProposals([hit.id], { edits });
    return { ...out, message: 'Accepted: written as your guidance', applied: out.accepted };
  });
  const reject = () => act(async () => {
    const out = await memoryService.rejectProposals([hit.id], why);
    return { ...out, message: 'Rejected, with your reason', applied: out.rejected };
  });

  return (
    <Paper variant="outlined" sx={{ p: 1.5, display: 'flex', gap: 1, borderStyle: 'dashed' }}>
      {canReview && (
        <Checkbox size="small" checked={!!checked} onChange={(e) => onCheck(e.target.checked)}
          slotProps={{ input: { 'aria-label': `Select proposal: ${hit.title || hit.id}` } }} sx={{ p: 0.25, alignSelf: 'flex-start' }} />
      )}
      <Box sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Typography variant="caption" color="text.secondary">
          Proposed guidance · {hit.author?.replace(/ \(model:.*\)$/, '')}{repoOf(hit) && ` · ${repoOf(hit)}`}
        </Typography>
        <Typography variant="body2" sx={{ fontWeight: 600 }}>{hit.title || hit.summary}</Typography>
        {mode === 'edit' ? (
          <>
            <TextField multiline minRows={3} size="small" label="Guidance" value={text} onChange={(e) => setText(e.target.value)} />
            <RadioGroup row value={scope} onChange={(e) => setScope(e.target.value)} aria-label="Guidance scope">
              <FormControlLabel value="all" control={<Radio size="small" />} label={<Typography variant="body2">All scanners</Typography>} />
              <FormControlLabel value="asvs" control={<Radio size="small" />} label={<Typography variant="body2">ASVS only</Typography>} />
              <FormControlLabel value="glasswing" control={<Radio size="small" />} label={<Typography variant="body2">Glasswing only</Typography>} />
            </RadioGroup>
            <FormControlLabel control={<Checkbox size="small" checked={always} onChange={(e) => setAlways(e.target.checked)} />} label={<Typography variant="body2">Always use</Typography>} />
          </>
        ) : (
          entry ? <EntryBody body={entry.body} /> : <CircularProgress size={16} />
        )}
        {mode === 'reject' && (
          <TextField size="small" label="Why it's wrong (later proposals are checked against it)" value={why} onChange={(e) => setWhy(e.target.value)} />
        )}
        {canReview && (
          <Stack direction="row" spacing={1}>
            {mode !== 'reject' && (
              <Button size="small" variant="contained" disabled={busy || (mode === 'edit' && !text.trim())} onClick={accept} sx={darkBtn}>
                {mode === 'edit' ? 'Accept as edited' : 'Accept'}
              </Button>
            )}
            {mode === null && <Button size="small" onClick={() => setMode('edit')} sx={{ textTransform: 'none' }}>Edit</Button>}
            {mode === 'reject' ? (
              <Button size="small" variant="outlined" color="error" disabled={busy || !why.trim()} onClick={reject} sx={{ textTransform: 'none' }}>Reject</Button>
            ) : (
              <Button size="small" onClick={() => setMode('reject')} sx={{ textTransform: 'none', color: '#b91c1c' }}>Reject…</Button>
            )}
            {mode && <Button size="small" onClick={() => setMode(null)} sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>}
          </Stack>
        )}
        {error && <Alert severity="error">{error}</Alert>}
      </Box>
    </Paper>
  );
}

function ProposalBatchBar({ ids, onDone, onClear }) {
  const [why, setWhy] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (reject) => {
    setBusy(true);
    setError(null);
    try {
      const out = reject ? await memoryService.rejectProposals(ids, why) : await memoryService.acceptProposals(ids, { why });
      onDone({ ...out, applied: reject ? out.rejected : out.accepted,
        message: `${reject ? 'Rejected' : 'Accepted'} ${(reject ? out.rejected : out.accepted).length} proposals` });
    } catch (e) { setError(e.message); }
    setBusy(false);
  };
  return (
    <Box sx={{ position: 'sticky', bottom: 0, zIndex: 2, mt: 2, bgcolor: '#fff', borderTop: '1px solid #d4d4d8', px: 2, py: 1.5, display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>{ids.length} proposals selected</Typography>
      <TextField size="small" label="Why (required to reject)" value={why} onChange={(e) => setWhy(e.target.value)} sx={{ flex: '1 1 300px' }} />
      <Button variant="contained" disabled={busy} onClick={() => run(false)} sx={darkBtn}>Accept {ids.length}</Button>
      <Button variant="outlined" color="error" disabled={busy || !why.trim()} onClick={() => run(true)} sx={{ textTransform: 'none' }}>Reject {ids.length}</Button>
      <Button onClick={onClear} sx={{ textTransform: 'none', color: '#52525b' }}>Clear</Button>
      {error && <Alert severity="error" sx={{ flexBasis: '100%' }}>{error}</Alert>}
    </Box>
  );
}

// The repo's guidance digest: what scans are given now, the draft after
// it and what it changes, with Accept. The next scan of the repo drafts a
// new version when decisions are newer than the accepted one.
export function DigestPanel({ domain, repo, canReview }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    memoryService.digest(domain, repo).then(setView, (e) => setError(e.message));
  }, [domain, repo]);
  useEffect(() => { load(); }, [load]);
  if (error) return <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>;
  if (!view) return null;
  const { current, draft, diff, pending } = view;
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 2 }} component="section" aria-label={`Guidance digest for ${repo}`}>
      <Typography variant="subtitle2">Guidance digest · {repo}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
        {current ? <>Scans use the version accepted {current.created?.slice(0, 10)} (<RouterLink to={`/memories/entries/${encodeURIComponent(current.id)}`}>{current.id}</RouterLink>).</>
          : 'No digest accepted yet: scans get the guidance entries one by one.'}
        {pending > 0 && (current
          ? ` ${pending} ${pending === 1 ? 'decision is' : 'decisions are'} newer than it; the next scan of ${repo} drafts a new version for you to accept here.`
          : ` ${pending} ${pending === 1 ? 'decision' : 'decisions'} so far; the next scan of ${repo} drafts one for you to accept here.`)}
      </Typography>
      {draft && (
        <Box sx={{ mt: 1.5 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            A new version is waiting: {diff.added.length} added, {diff.removed.length} removed, {diff.kept.length} unchanged
          </Typography>
          <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, fontSize: 13 }}>
            {diff.added.map((r) => <li key={`a${r}`} style={{ color: '#065f46' }}>Added: {r.replace(/^- /, '')}</li>)}
            {diff.removed.map((r) => <li key={`r${r}`} style={{ color: '#b91c1c' }}>Removed: {r.replace(/^- /, '')}</li>)}
          </Box>
          {canReview && (
            <Button size="small" variant="contained" disabled={busy} sx={{ ...darkBtn, mt: 1 }} onClick={async () => {
              setBusy(true);
              try { await memoryService.acceptDigest(draft.id); load(); } catch (e) { setError(e.message); }
              setBusy(false);
            }}>
              Accept this version
            </Button>
          )}
          <Typography variant="caption" component="div" color="text.secondary" sx={{ mt: 0.5 }}>
            Until you accept it, scans keep the last accepted version.
          </Typography>
        </Box>
      )}
    </Paper>
  );
}

export default function TriageTab({ domain, run, canReview, onReviewed }) {
  const listRef = useRef(null);
  const [hits, setHits] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('needs');
  const [selected, setSelected] = useState(null);
  const [checked, setChecked] = useState(() => new Set());
  const [result, setResult] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out = await memoryService.search(domain, { run, includeInactive: true, limit: 1000, order: 'oldest' });
      setHits(out.hits.filter((h) => isFinding(h) || isProposal(h)));
    } catch (e) {
      setError(e.message);
      setHits([]);
    }
  }, [domain, run]);
  useEffect(() => { load(); }, [load]);

  const shown = useMemo(() => matchesIn(hits || [], filter), [hits, filter]);
  useEffect(() => {
    setSelected((cur) => (shown.some((h) => h.id === cur) ? cur : shown[0]?.id || null));
    setChecked((cur) => new Set([...cur].filter((id) => shown.some((h) => h.id === id))));
  }, [shown]);
  const repos = useMemo(() => [...new Set((hits || []).map(repoOf).filter(Boolean))], [hits]);

  const done = async (out) => {
    setResult(out);
    setChecked(new Set());
    await load();
    onReviewed?.();
  };
  const toggle = (id, on) => setChecked((cur) => {
    const next = new Set(cur);
    if (on) next.add(id); else next.delete(id);
    return next;
  });
  const onKeyDown = (e) => {
    if (['INPUT', 'TEXTAREA'].includes(e.target.tagName) || e.metaKey || e.ctrlKey) return;
    const i = shown.findIndex((h) => h.id === selected);
    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      const next = shown[Math.min(shown.length - 1, Math.max(0, i + (e.key === 'j' ? 1 : -1)))];
      if (next) setSelected(next.id);
    } else if (e.key === 'x' && canReview && shown[i] && !shown[i].status) {
      e.preventDefault();
      toggle(shown[i].id, !checked.has(shown[i].id));
    }
  };

  if (hits === null) return <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>;
  const current = shown.find((h) => h.id === selected);
  const checkedHits = shown.filter((h) => checked.has(h.id));
  const counts = Object.fromEntries(FILTERS.map((f) => [f.key, matchesIn(hits, f.key).length]));

  return (
    <Box>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {repos.map((r) => <DigestPanel key={r} domain={domain} repo={r} canReview={canReview} />)}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1.5 }} role="tablist" aria-label="Which findings">
        {FILTERS.filter((f) => f.key !== 'unlocated' || counts.unlocated > 0).map((f) => (
          <Chip
            key={f.key} role="tab" aria-selected={filter === f.key} clickable
            label={`${f.label} · ${counts[f.key]}`} onClick={() => setFilter(f.key)}
            sx={filter === f.key ? { bgcolor: '#18181b', color: '#fff', '&:hover': { bgcolor: '#27272a' } } : { bgcolor: '#fff', border: '1px solid #d4d4d8' }}
          />
        ))}
      </Box>
      {result && <Result key={result.batch} result={result} onUndone={async () => { await load(); onReviewed?.(); }} />}
      {hits.length === 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
          This run wrote no findings or proposals here.
        </Typography>
      )}
      {hits.length > 0 && filter === 'proposals' && (
        <Stack spacing={1}>
          {shown.length === 0 && <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>No proposals waiting.</Typography>}
          {shown.map((h) => (
            <ProposalCard key={h.id} hit={h} canReview={canReview} checked={checked.has(h.id)}
              onCheck={(on) => toggle(h.id, on)} onDone={done} />
          ))}
          <Typography variant="caption" color="text.secondary">
            Accepting writes the guidance as your entry, scoped to the repo. The proposal is kept, linked, so you can see where guidance came from.
          </Typography>
          {canReview && checked.size > 0 && (
            <ProposalBatchBar ids={[...checked]} onDone={done} onClear={() => setChecked(new Set())} />
          )}
        </Stack>
      )}
      {hits.length > 0 && filter !== 'proposals' && (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '5fr 7fr' }, gap: 2, alignItems: 'start' }}>
          <Stack spacing={1} ref={listRef} tabIndex={0} onKeyDown={onKeyDown} aria-label="Findings" sx={{ outline: 'none' }}>
            {canReview && filter !== 'decided' && (
              <Typography variant="caption" sx={{ color: '#71717a' }}>j / k move · x select</Typography>
            )}
            {shown.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>Nothing here.</Typography>
            )}
            {shown.map((h) => (
              <FindingRow
                key={h.id} hit={h} selected={h.id === selected}
                onClick={() => { setSelected(h.id); listRef.current?.focus(); }}
                checked={checked.has(h.id)}
                onCheck={canReview && filter !== 'decided' ? (on) => toggle(h.id, on) : undefined}
              />
            ))}
          </Stack>
          <Box>
            {checkedHits.length > 1 ? (
              <Paper variant="outlined" sx={{ p: 2.5 }}>
                <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>{checkedHits.length} findings selected</Typography>
                <DispositionForm key={checkedHits.map((h) => h.id).join(',')} hits={checkedHits} batch onDone={done} />
              </Paper>
            ) : current ? (
              <FindingDetail hit={current} canReview={canReview} onDone={done} />
            ) : null}
          </Box>
        </Box>
      )}
    </Box>
  );
}
