// The bar that appears when entries are ticked: one decision and one reason
// for all of them, then what happened, with Undo (B11).
import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  FormControlLabel,
  Radio,
  RadioGroup,
  TextField,
  Typography,
} from '@mui/material';
import memoryService from '../../services/memoryService';

const ACTIONS = [
  { action: 'curate', label: 'Curate' },
  { action: 'verify', label: 'Verify' },
  { action: 'keep', label: 'Keep raw' },
  { action: 'demote', label: 'Demote' },
];

export function BatchResult({ result, onUndone }) {
  const [undoing, setUndoing] = useState(false);
  const [undo, setUndo] = useState(null);
  const verb = { curate: 'Curated', verify: 'Verified', keep: 'Kept', demote: 'Demoted' }[result.action] || 'Reviewed';

  const doUndo = async () => {
    setUndoing(true);
    try {
      const out = await memoryService.undoBatch(result.batch);
      setUndo(out);
      onUndone?.();
    } catch (e) {
      setUndo({ error: e.message });
    } finally {
      setUndoing(false);
    }
  };

  if (undo) {
    return (
      <Alert severity={undo.error ? 'error' : 'info'} sx={{ mb: 2 }}>
        {undo.error || `Undid ${undo.undone.length} of the batch.`}
        {undo.skipped?.length > 0 && ` ${undo.skipped.length} left as they are: changed since.`}
      </Alert>
    );
  }
  return (
    <Alert
      severity={result.applied.length ? 'success' : 'warning'}
      role="status"
      sx={{ mb: 2 }}
      action={result.applied.length > 0 && (
        <Button color="inherit" size="small" onClick={doUndo} disabled={undoing}>
          {undoing ? <CircularProgress size={14} color="inherit" /> : 'Undo batch'}
        </Button>
      )}
    >
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {verb} {result.applied.length} {result.applied.length === 1 ? 'entry' : 'entries'}
        {result.restored?.length > 0 && `, and brought back ${result.restored.length} they had superseded`}
      </Typography>
      <Typography variant="caption" component="div">
        Each has its own review record with your reason and batch <code>{result.batch}</code>.
      </Typography>
      {result.skipped?.length > 0 && (
        <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, fontSize: 13 }}>
          {result.skipped.map((s) => <li key={s.id}><code>{s.id}</code>: {s.reason}</li>)}
        </Box>
      )}
    </Alert>
  );
}

export default function BatchBar({ ids, why, setWhy, onDone, onClear }) {
  const [action, setAction] = useState('demote');
  const [restore, setRestore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const needsWhy = action !== 'keep';
  const label = ACTIONS.find((a) => a.action === action)?.label;

  const apply = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await memoryService.batchReview({ ids, action, why, restoreSuperseded: action === 'demote' && restore });
      onDone(out);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Box
      component="form" onSubmit={apply} aria-label="Decide on the selected entries"
      sx={{ position: 'sticky', bottom: 0, zIndex: 2, mt: 2, bgcolor: '#fff', borderTop: '1px solid #d4d4d8', boxShadow: '0 -4px 12px rgba(0,0,0,0.04)', px: 2, py: 1.5, display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}
    >
      <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 92 }}>{ids.length} selected</Typography>
      <RadioGroup row value={action} onChange={(e) => setAction(e.target.value)} aria-label="Decision">
        {ACTIONS.map((a) => (
          <FormControlLabel key={a.action} value={a.action} control={<Radio size="small" />} label={a.label}
            sx={{ mr: 1, '& .MuiFormControlLabel-label': { fontSize: 14, fontWeight: action === a.action ? 600 : 400 } }} />
        ))}
      </RadioGroup>
      <TextField
        size="small" label={needsWhy ? 'Why (recorded on each entry)' : 'Why (optional)'} value={why}
        onChange={(e) => setWhy(e.target.value)} sx={{ flex: '1 1 320px' }}
      />
      {action === 'demote' && (
        <FormControlLabel
          control={<Checkbox size="small" checked={restore} onChange={(e) => setRestore(e.target.checked)} />}
          label="Bring back what these superseded"
          sx={{ '& .MuiFormControlLabel-label': { fontSize: 13 } }}
        />
      )}
      <Box sx={{ display: 'flex', gap: 1 }}>
        <Button type="submit" variant="contained" disabled={busy || (needsWhy && !why.trim())}
          sx={{ bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } }}>
          {busy ? <CircularProgress size={16} color="inherit" /> : `${label} ${ids.length} ${ids.length === 1 ? 'entry' : 'entries'}`}
        </Button>
        <Button onClick={onClear} sx={{ textTransform: 'none', color: '#52525b' }}>Clear</Button>
      </Box>
      {error && <Alert severity="error" sx={{ flexBasis: '100%' }}>{error}</Alert>}
    </Box>
  );
}
