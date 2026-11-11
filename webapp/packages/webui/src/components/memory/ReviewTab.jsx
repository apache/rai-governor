// Review: raw and overdue entries, oldest first, and a decision on each.
//
// Whoever wrote an entry cannot review it; the API refuses too.
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
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
import BlockIcon from '@mui/icons-material/Block';
import memoryService from '../../services/memoryService';
import { HitCard } from './BrowseTab';
import EntryPanel from './EntryPanel';
import { TrustDisc } from './memoryBits';
import { choicesFor, isOverdue, plusDays } from './memoryFormat';

function Decision({ entry, you, kind, onDone, onSkip }) {
  const choices = choicesFor(entry.trust_tier);
  const [action, setAction] = useState(choices[0].action);
  const [why, setWhy] = useState('');
  const [reviewBy, setReviewBy] = useState(plusDays(90));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const own = you?.author === entry.author;
  const allowed = Boolean(you?.canReview) && !own;
  const choice = choices.find((c) => c.action === action) || choices[0];
  const needsWhy = action !== 'keep';

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await memoryService.review(entry.id, { action, why, reviewBy });
      onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Paper variant="outlined" sx={{ p: 3 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Your decision</Typography>
      <Typography variant="caption" color="text.secondary">Trust after review</Typography>
      <RadioGroup value={action} onChange={(e) => setAction(e.target.value)} sx={{ my: 1, gap: 0.75 }}>
        {choices.map((c) => (
          <Paper
            key={c.action} variant="outlined"
            sx={{ px: 1.5, py: 0.25, borderColor: c.action === action ? '#18181b' : undefined }}
          >
            <FormControlLabel
              value={c.action}
              control={<Radio size="small" />}
              disabled={!allowed}
              label={(
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  {c.disc ? <TrustDisc tier={c.disc} /> : <BlockIcon sx={{ fontSize: 12 }} />}
                  <Typography variant="body2"><b>{c.label}</b>: {c.help}</Typography>
                </Box>
              )}
            />
          </Paper>
        ))}
      </RadioGroup>
      <TextField
        label={needsWhy ? 'Why (recorded with the decision)' : 'Note (optional, recorded with the decision)'}
        multiline minRows={2} fullWidth size="small"
        placeholder="What you checked, and against what"
        value={why} onChange={(e) => setWhy(e.target.value)}
        disabled={!allowed}
        sx={{ mb: 2 }}
      />
      {action !== 'demote' && (
        <TextField
          label="Next review by" type="date" size="small"
          value={reviewBy} onChange={(e) => setReviewBy(e.target.value)}
          disabled={!allowed}
          slotProps={{ inputLabel: { shrink: true } }}
          sx={{ mb: 2, width: 200, display: 'flex' }}
        />
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Stack direction="row" spacing={1} alignItems="center">
        <Button
          variant="contained" onClick={submit}
          disabled={!allowed || busy || (needsWhy && !why.trim())}
        >
          {busy ? <CircularProgress size={18} color="inherit" /> : `${choice.verb} entry`}
        </Button>
        <Button variant="outlined" onClick={onSkip} disabled={busy}>Skip</Button>
        {allowed && (
          <Typography variant="caption" color="text.secondary">Recorded as {you.author}</Typography>
        )}
      </Stack>
      {own && (
        <Alert severity="info" icon={<BlockIcon fontSize="small" />} sx={{ mt: 2 }}>
          You wrote this entry, so someone else has to review it. The approver is never the proposer.
        </Alert>
      )}
      {!own && !you?.canReview && (
        <Alert severity="info" sx={{ mt: 2 }}>
          {kind === 'project'
            ? "Only the project's PMC reviews entries in its domain."
            : 'Only memory writers review entries.'}
        </Alert>
      )}
    </Paper>
  );
}

const FILTERS = [
  { key: 'all', label: 'All', test: (h) => h.trust_tier === 'raw' || isOverdue(h) },
  { key: 'raw', label: 'Raw', test: (h) => h.trust_tier === 'raw' },
  { key: 'due', label: 'Review overdue', test: (h) => isOverdue(h) },
];

export default function ReviewTab({ domain, kind, you, initialEntry, onReviewed }) {
  const [rows, setRows] = useState(null);
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState(initialEntry || null);
  const [error, setError] = useState(null);
  const [shown, setShown] = useState(25);

  const load = useCallback(async () => {
    setError(null);
    try {
      // Only what's waiting for review, chosen on the server: the oldest 200
      // of everything could be all reviewed entries in a busy domain.
      const out = await memoryService.search(domain, { order: 'oldest', queue: true, limit: 1000 });
      setRows(out.hits);
    } catch (e) {
      setError(e.message);
      setRows([]);
    }
  }, [domain]);

  useEffect(() => { load(); }, [load]);

  const queue = useMemo(
    () => (rows || []).filter(FILTERS.find((f) => f.key === filter).test),
    [rows, filter],
  );

  // Keep a selection that's in the queue (or was asked for by link).
  useEffect(() => {
    if (!rows) return;
    if (selected && (rows.some((r) => r.id === selected) || selected === initialEntry)) return;
    setSelected(queue[0]?.id || null);
  }, [rows, queue, selected, initialEntry]);

  const after = () => {
    const i = queue.findIndex((r) => r.id === selected);
    return queue[i + 1]?.id || null;
  };

  const next = () => setSelected(after() || queue[0]?.id || null);

  // The reviewed entry may leave the queue, so pick the next one first.
  const reviewed = async () => {
    const nextId = after();
    await load();
    onReviewed?.();
    setSelected(nextId);
  };

  // An entry opened by link may not be in the queue (kept, not yet due):
  // read it on its own so it can still be decided on.
  const [linked, setLinked] = useState(null);
  const inRows = (rows || []).find((r) => r.id === selected);
  useEffect(() => {
    if (!rows || !selected || inRows) { setLinked(null); return undefined; }
    let live = true;
    memoryService.get(selected).then(
      (out) => { if (live) setLinked({ ...out.frontmatter, id: out.frontmatter.id || selected }); },
      () => { if (live) setLinked(null); },
    );
    return () => { live = false; };
  }, [rows, selected, inRows]);
  const current = inRows || (linked?.id === selected ? linked : null);

  if (rows === null) {
    return <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>;
  }

  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '5fr 7fr' }, gap: 2, alignItems: 'start' }}>
      <Stack spacing={1}>
        <Stack direction="row" spacing={1}>
          {FILTERS.map((f) => {
            const n = rows.filter(f.test).length;
            return (
              <Chip
                key={f.key} label={`${f.label} · ${n}`}
                onClick={() => setFilter(f.key)}
                color={filter === f.key ? 'primary' : 'default'}
                variant={filter === f.key ? 'filled' : 'outlined'}
                sx={filter === f.key ? { bgcolor: '#18181b' } : { bgcolor: '#fff' }}
              />
            );
          })}
        </Stack>
        <Typography variant="caption" color="text.secondary">
          Oldest first. Nothing below raw is acted on unchecked.
        </Typography>
        {error && <Alert severity="error">{error}</Alert>}
        {queue.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>Nothing waiting for review.</Typography>
        )}
        {queue.slice(0, shown).map((hit) => (
          <HitCard
            key={hit.id} hit={hit} selected={hit.id === selected} onClick={() => setSelected(hit.id)}
            extra={you?.author === hit.author && (
              <Chip label="You wrote this" size="small" variant="outlined" sx={{ height: 18, fontSize: '0.68rem' }} />
            )}
          />
        ))}
        {queue.length > shown && (
          <Button size="small" onClick={() => setShown(shown + 25)}>{queue.length - shown} more</Button>
        )}
      </Stack>
      {selected && (
        <Stack spacing={2}>
          <EntryPanel id={selected} />
          {current && (
            <Decision
              key={current.id}
              entry={current}
              you={you}
              kind={kind}
              onDone={reviewed}
              onSkip={next}
            />
          )}
        </Stack>
      )}
    </Box>
  );
}
