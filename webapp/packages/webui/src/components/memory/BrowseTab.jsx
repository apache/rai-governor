// Browse: search a domain's summaries, read one entry beside the list.
//
// Entries can be ticked and decided on together (B11), and the list can be
// one run's entries (?run=). Keys, with the list focused: j and k move, x
// ticks, c v r d curate, verify, keep raw or demote the entry shown, with
// the reason in the bar.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  FormControlLabel,
  InputAdornment,
  MenuItem,
  Paper,
  Stack,
  TextField,
  ToggleButton,
  Typography,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import memoryService from '../../services/memoryService';
import { EntryMetaLine } from './memoryBits';
import EntryPanel from './EntryPanel';
import { today } from './memoryFormat';
import BatchBar, { BatchResult } from './BatchBar';
import GuidanceView from './GuidanceView';

const TYPES = ['decision', 'fact', 'convention', 'method', 'handoff', 'policy'];
const TIERS = ['raw', 'curated', 'verified'];
// Entries a scan writes for reuse by its next run: its security profile,
// domain map and false-positive guidance. "Demote its findings" leaves them.
const REUSABLE = ['convention', 'method'];
const KEY_ACTIONS = { c: 'curate', v: 'verify', r: 'keep', d: 'demote' };

export function HitCard({ hit, selected, onClick, extra, checked, onCheck }) {
  const inactive = Boolean(hit.status);
  return (
    <Paper
      variant="outlined"
      onClick={onClick}
      sx={{
        p: 1.5,
        cursor: 'pointer',
        borderColor: selected ? '#18181b' : undefined,
        borderWidth: selected ? 1.5 : 1,
        borderStyle: inactive ? 'dashed' : 'solid',
        bgcolor: inactive ? '#fafafa' : '#fff',
        display: 'flex',
        gap: 1,
        alignItems: 'flex-start',
        '&:hover': { borderColor: '#71717a' },
      }}
    >
      {onCheck && (
        <Checkbox
          size="small" checked={!!checked} disabled={inactive}
          onClick={(e) => e.stopPropagation()} onChange={(e) => onCheck(e.target.checked)}
          slotProps={{ input: { 'aria-label': `Select: ${hit.title || hit.id}` } }}
          sx={{ p: 0.25, mt: -0.25 }}
        />
      )}
      <Box sx={{ minWidth: 0 }}>
        <EntryMetaLine hit={hit} extra={extra} />
        <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.5, textDecoration: hit.status === 'demoted' ? 'line-through' : 'none', color: inactive ? '#52525b' : 'inherit' }}>
          {hit.title || hit.id}
        </Typography>
        {hit.summary && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>{hit.summary}</Typography>
        )}
      </Box>
    </Paper>
  );
}

export default function BrowseTab({ domain, run, select, canReview, onReviewed }) {
  const navigate = useNavigate();
  const listRef = useRef(null);
  const [checked, setChecked] = useState(() => new Set());
  const [why, setWhy] = useState('');
  const [result, setResult] = useState(null);
  const [keyMsg, setKeyMsg] = useState(null);
  const preselected = useRef(false);
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [type, setType] = useState('');
  const [trustTier, setTrustTier] = useState('');
  const [due, setDue] = useState(false);
  const [inactive, setInactive] = useState(Boolean(run));
  const [hits, setHits] = useState(null);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);
  // Guidance for a repo (scans that remember): a view of its own.
  const [repos, setRepos] = useState([]);
  const [guidanceRepo, setGuidanceRepo] = useState('');

  // Every domain the caller can read (the Memory page's "All my domains").
  const all = domain === '*';

  useEffect(() => {
    if (all) return undefined;
    let live = true;
    memoryService.guidanceRepos(domain).then((o) => { if (live) setRepos(o.repos || []); }, () => {});
    return () => { live = false; };
  }, [domain, all]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out = await memoryService.search(domain, {
        q: query, type, trustTier, dueBy: due ? today() : undefined, includeInactive: inactive,
        run, limit: run ? 1000 : undefined,
      });
      setHits(out.hits);
      setSelected((cur) => (out.hits.some((h) => h.id === cur) ? cur : out.hits[0]?.id || null));
      setChecked((cur) => new Set([...cur].filter((id) => out.hits.some((h) => h.id === id && !h.status))));
      // "Demote its findings" from a run: tick the findings, leave what
      // the next run reuses. Once, on arrival.
      if (select === 'findings' && !preselected.current) {
        preselected.current = true;
        // Findings are facts; decisions, handoffs and policies aren't findings.
        setChecked(new Set(out.hits.filter((h) => !h.status && h.type === 'fact').map((h) => h.id)));
        setWhy((w) => w || 'Findings from a run that did not complete properly.');
      }
    } catch (e) {
      setError(e.message);
      setHits([]);
    }
  }, [domain, query, type, trustTier, due, inactive, run, select]);

  useEffect(() => { load(); }, [load]);

  const live = (hits || []).filter((h) => !h.status);
  const allChecked = live.length > 0 && live.every((h) => checked.has(h.id));
  const toggle = (id, on) => setChecked((cur) => {
    const next = new Set(cur);
    if (on) next.add(id); else next.delete(id);
    return next;
  });

  const onKeyDown = async (e) => {
    if (!hits?.length || e.metaKey || e.ctrlKey || e.altKey) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
    const i = hits.findIndex((h) => h.id === selected);
    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      const next = hits[Math.min(hits.length - 1, Math.max(0, i + (e.key === 'j' ? 1 : -1)))];
      if (next) setSelected(next.id);
      return;
    }
    const hit = hits[i];
    if (!hit) return;
    if (e.key === 'x' && canReview && !hit.status) {
      e.preventDefault();
      toggle(hit.id, !checked.has(hit.id));
      return;
    }
    const action = KEY_ACTIONS[e.key];
    if (action && canReview) {
      e.preventDefault();
      if (action !== 'keep' && !why.trim()) {
        setKeyMsg('Write the reason in the bar below first: it is recorded with the decision.');
        return;
      }
      try {
        await memoryService.review(hit.id, { action, why });
        setKeyMsg(`${action === 'keep' ? 'Kept' : `${action[0].toUpperCase()}${action.slice(1)}d`}: ${hit.title || hit.id}`);
        const next = hits[i + 1];
        await load();
        if (next) setSelected(next.id);
        onReviewed?.();
      } catch (err) {
        setKeyMsg(err.message);
      }
    }
  };

  const batchDone = async (out) => {
    setResult(out);
    setChecked(new Set());
    await load();
    onReviewed?.();
  };

  return (
    <Box>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1} sx={{ mb: 2 }}>
        <TextField
          size="small" fullWidth placeholder="Search titles, summaries, repositories, runs and finding ids"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') setQuery(q.trim()); }}
          onBlur={() => setQuery(q.trim())}
          slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> } }}
          sx={{ bgcolor: '#fff' }}
        />
        <TextField
          select size="small" label="Type" value={type} onChange={(e) => setType(e.target.value)}
          sx={{ minWidth: 130, bgcolor: '#fff' }}
        >
          <MenuItem value="">any</MenuItem>
          {TYPES.map((t) => <MenuItem key={t} value={t}>{t}</MenuItem>)}
        </TextField>
        <TextField
          select size="small" label="Trust" value={trustTier} onChange={(e) => setTrustTier(e.target.value)}
          sx={{ minWidth: 130, bgcolor: '#fff' }}
        >
          <MenuItem value="">any</MenuItem>
          {TIERS.map((t) => <MenuItem key={t} value={t}>{t}</MenuItem>)}
        </TextField>
        <ToggleButton
          size="small" value="due" selected={due} onChange={() => setDue(!due)}
          sx={{ whiteSpace: 'nowrap', textTransform: 'none', bgcolor: '#fff' }}
        >
          Due for review
        </ToggleButton>
        <ToggleButton
          size="small" value="inactive" selected={inactive} onChange={() => setInactive(!inactive)}
          sx={{ whiteSpace: 'nowrap', textTransform: 'none', bgcolor: '#fff' }}
        >
          {run ? 'Show demoted' : 'Show superseded'}
        </ToggleButton>
        {repos.length > 0 && !run && (
          <TextField
            select size="small" label="Guidance for" value={guidanceRepo} onChange={(e) => setGuidanceRepo(e.target.value)}
            sx={{ minWidth: 220, bgcolor: '#fff' }}
          >
            <MenuItem value="">all entries</MenuItem>
            {repos.map((r) => <MenuItem key={r} value={r}>{r}</MenuItem>)}
          </TextField>
        )}
      </Stack>
      {guidanceRepo && <GuidanceView domain={domain} repo={guidanceRepo} canReview={canReview} />}
      {!guidanceRepo && (<>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {result && <BatchResult key={result.batch} result={result} onUndone={async () => { await load(); onReviewed?.(); }} />}
      {keyMsg && <Alert severity="info" onClose={() => setKeyMsg(null)} sx={{ mb: 2 }}>{keyMsg}</Alert>}

      {hits === null ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>
      ) : (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '5fr 7fr' }, gap: 2, alignItems: 'start' }}>
          <Stack spacing={1} ref={listRef} tabIndex={0} onKeyDown={onKeyDown} aria-label="Entries" sx={{ outline: 'none' }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              {canReview && live.length > 0 ? (
                <FormControlLabel
                  control={(
                    <Checkbox size="small" checked={allChecked} indeterminate={!allChecked && checked.size > 0}
                      onChange={(e) => setChecked(e.target.checked ? new Set(live.map((h) => h.id)) : new Set())} />
                  )}
                  label={<Typography variant="caption">Select all {live.length}</Typography>}
                  sx={{ m: 0 }}
                />
              ) : (
                <Typography variant="caption" color="text.secondary">
                  {hits.length} {hits.length === 1 ? 'entry' : 'entries'} · summaries only; open one to read it
                </Typography>
              )}
              {canReview && (
                <Typography variant="caption" sx={{ color: '#71717a' }}>j / k move · x select · c v r d decide</Typography>
              )}
            </Box>
            {hits.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                {query ? 'Nothing matches.' : `Nothing recorded in ${all ? 'your domains' : 'this domain'} yet.`}
              </Typography>
            )}
            {hits.map((hit) => (
              <HitCard
                key={hit.id} hit={hit} selected={hit.id === selected}
                onClick={() => { setSelected(hit.id); listRef.current?.focus(); }}
                checked={checked.has(hit.id)}
                onCheck={canReview ? (on) => toggle(hit.id, on) : undefined}
                extra={all ? (
                  <Chip label={hit.domain} size="small" sx={{ height: 18, fontSize: '0.68rem', bgcolor: '#f4f4f5', border: '1px solid #e4e4e7' }} />
                ) : run && REUSABLE.includes(hit.type) && !hit.status ? (
                  <Chip label={select === 'findings' ? 'Reusable: kept out of the batch' : 'Reusable'} size="small"
                    sx={{ height: 18, fontSize: '0.68rem', bgcolor: '#eff6ff', color: '#1e40af', border: '1px solid #bfdbfe' }} />
                ) : null}
              />
            ))}
          </Stack>
          {selected && (
            <EntryPanel
              id={selected}
              actions={(entry, history) => (
                <Stack direction="row" spacing={1}>
                  <Button variant="contained" size="small" onClick={() => navigate(`/memories/entries/${encodeURIComponent(entry.id)}`)}>
                    Open full entry
                  </Button>
                  {history && (
                    <Button variant="outlined" size="small" onClick={() => navigate(`/memories/entries/${encodeURIComponent(entry.id)}#history`)}>
                      History · {history.versions.length} {history.versions.length === 1 ? 'version' : 'versions'}
                    </Button>
                  )}
                </Stack>
              )}
            />
          )}
        </Box>
      )}

      {canReview && checked.size > 0 && (
        <BatchBar ids={[...checked]} why={why} setWhy={setWhy} onDone={batchDone} onClear={() => setChecked(new Set())} />
      )}
      {canReview && checked.size === 0 && hits?.length > 0 && (
        <TextField
          size="small" fullWidth label="Reason for decisions made with c, v, r and d" value={why}
          onChange={(e) => setWhy(e.target.value)} sx={{ mt: 2, bgcolor: '#fff', maxWidth: 640 }}
        />
      )}
      </>)}
    </Box>
  );
}
