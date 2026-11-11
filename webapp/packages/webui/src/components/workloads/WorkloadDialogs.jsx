// What a project's PMC does to its Workloads page: add a workload from the
// catalog, set the project's defaults for it, or take it off the page.
import React, { useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  TextField,
  Typography,
} from '@mui/material';
import workloadService from '../../services/workloadService';
import InputField from './InputField';

function DefaultsFields({ inputs, values, setValues }) {
  if (!inputs.length) return null;
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5 }}>
      {inputs.map((i) => {
        // What a blank leaves: the workload's own default (i.default may
        // already be this project's), or for a repository the first offered.
        const fallback = i.workloadDefault || (i.kind === 'repository' ? i.choices?.[0] : '');
        return (
          <Box key={i.name} sx={{ gridColumn: i.kind === 'repository' ? '1 / -1' : 'auto' }}>
            <InputField
              input={i} value={values[i.name] ?? ''}
              onChange={(v) => setValues((cur) => ({ ...cur, [i.name]: v }))}
              blankLabel={fallback ? `The workload's: ${fallback}` : 'No default'}
              helperText={i.kind === 'repository' ? 'What members see first. They can pick another of the project\'s.' : ' '}
            />
          </Box>
        );
      })}
    </Box>
  );
}

const DARK = { bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } };

export function AddWorkloadDialog({ open, project, available, onClose, onAdded }) {
  const [id, setId] = useState(available[0]?.id || '');
  const [values, setValues] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const chosen = useMemo(() => available.find((a) => a.id === id), [available, id]);

  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      await workloadService.add(id, project, values);
      onAdded();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth aria-labelledby="add-workload-title">
      <DialogTitle id="add-workload-title">Add a workload to {project}</DialogTitle>
      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
        <TextField select size="small" label="Workload" value={id} onChange={(e) => { setId(e.target.value); setValues({}); }}>
          {available.map((a) => <MenuItem key={a.id} value={a.id}>{a.name}</MenuItem>)}
        </TextField>
        {chosen && (
          <>
            <Box>
              <Typography variant="body2" sx={{ color: '#3f3f46' }}>{chosen.description}</Typography>
              <Typography variant="caption" sx={{ color: '#71717a', display: 'block', mt: 0.5 }}>
                {[chosen.produces, chosen.typicalDuration && `Typical run: ${chosen.typicalDuration}`,
                  chosen.fixedInputNames?.length && `Set by a site admin: ${chosen.fixedInputNames.join(', ')}`].filter(Boolean).join(' · ')}
              </Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>{project}'s defaults (optional)</Typography>
              <DefaultsFields inputs={chosen.inputs} values={values} setValues={setValues} />
            </Box>
            <Typography variant="caption" sx={{ color: '#71717a' }}>
              Every member of {project} will see it on the Workloads page and can run it.
            </Typography>
          </>
        )}
        {error && <Alert severity="error">{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>
        <Button variant="contained" onClick={add} disabled={!chosen || busy} sx={DARK}>
          {busy ? <CircularProgress size={16} color="inherit" /> : `Add to ${project}`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function DefaultsDialog({ open, project, workload, onClose, onSaved }) {
  const [values, setValues] = useState(() => ({ ...(workload.projectDefaults || {}) }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await workloadService.setDefaults(workload.id, project, values);
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth aria-labelledby="defaults-title">
      <DialogTitle id="defaults-title">{workload.name}: {project}'s defaults</DialogTitle>
      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: '8px !important' }}>
        <DefaultsFields inputs={workload.inputs} values={values} setValues={setValues} />
        <Typography variant="caption" sx={{ color: '#71717a' }}>Members can still change any of these when they run it.</Typography>
        {error && <Alert severity="error">{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>
        <Button variant="contained" onClick={save} disabled={busy} sx={DARK}>
          {busy ? <CircularProgress size={16} color="inherit" /> : 'Save defaults'}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export function RemoveDialog({ open, project, workload, onClose, onRemoved }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await workloadService.remove(workload.id, project);
      onRemoved();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onClose={onClose} aria-labelledby="remove-title">
      <DialogTitle id="remove-title">Take {workload.name} off {project}'s page?</DialogTitle>
      <DialogContent>
        <Typography variant="body2">
          Members won't see it or run it. Its runs, their results and the memory they wrote stay. You can add it back from the catalog.
        </Typography>
        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>
        <Button variant="contained" color="error" onClick={remove} disabled={busy} sx={{ textTransform: 'none' }}>Take it off</Button>
      </DialogActions>
    </Dialog>
  );
}
