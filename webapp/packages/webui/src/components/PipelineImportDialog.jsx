// Import a pipeline from GitHub or a local folder (ISSUE-011).
//
// Two steps: Preview (a dry run that shows what each agent would become),
// then Import. By default an import lands in a labelled copy: the commit hash
// for GitHub, a timestamp for a folder, or a label you type. The same label
// again updates that copy; a new one makes another alongside. Unticking
// "Label this copy" imports into the stable copy instead: the plain agent
// names at /rest/{name}, updated in place by each unlabelled import. Every
// agent imported is also deployed, at /rest/{its friendly name}, unless
// unticked.
import React, { useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Typography,
} from '@mui/material';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import pipelineService from '../services/pipelineService';

const ACTION_STYLE = {
  create: { bgcolor: '#dcfce7', color: '#166534' },
  update: { bgcolor: '#fef9c3', color: '#854d0e' },
  unchanged: { bgcolor: '#f4f4f5', color: '#71717a' },
};

export function PlanTable({ plan }) {
  return (
    <Table size="small">
      <TableHead>
        <TableRow>
          <TableCell>Agent</TableCell>
          <TableCell>Stored as</TableCell>
          <TableCell>Action</TableCell>
          <TableCell>Changes</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {plan.agents.map((row) => (
          <TableRow key={row.agent}>
            <TableCell>
              {row.agent}
              {plan.entry === row.agent && (
                <Typography component="span" variant="caption" color="text.secondary"> (entry)</Typography>
              )}
            </TableCell>
            <TableCell><code>{row.name}</code></TableCell>
            <TableCell>
              <Chip label={row.action} size="small" sx={{ ...ACTION_STYLE[row.action], fontWeight: 500 }} />
            </TableCell>
            <TableCell>
              <Typography variant="body2" color="text.secondary">
                {row.changes.length ? row.changes.join(', ') : '—'}
                {row.externalCalls.length > 0 && ` · calls existing: ${row.externalCalls.join(', ')}`}
              </Typography>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function PipelineImportDialog({ open, onClose, onImported }) {
  const [tab, setTab] = useState('github');
  const [github, setGithub] = useState({ repo: '', ref: 'main', path: '', token: '' });
  const [files, setFiles] = useState(null);
  const [label, setLabel] = useState('');
  const [plan, setPlan] = useState(null);
  const [deploy, setDeploy] = useState(true);
  const [labeled, setLabeled] = useState(true);
  // After an import whose deployments didn't all go through: what failed.
  const [imported, setImported] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const folderInput = useRef(null);

  const reset = () => {
    setPlan(null);
    setError(null);
    setImported(null);
  };

  const close = () => {
    reset();
    setFiles(null);
    onClose();
  };

  const send = async (dryRun) => {
    setBusy(true);
    setError(null);
    try {
      // The import reuses the preview's label: for a folder the API makes a
      // fresh timestamp on every call, so without this the import would land
      // in a different copy from the one previewed.
      const useLabel = labeled ? (label || (dryRun ? undefined : plan?.label)) : undefined;
      const result = tab === 'github'
        ? await pipelineService.importFromGithub({ ...github, label: useLabel, dryRun, deploy, labeled })
        : await pipelineService.importFromFolder({ files, label: useLabel, dryRun, deploy, labeled });
      if (dryRun) {
        setPlan(result);
      } else if (result.deployProblems?.length) {
        // Imported, but some agents weren't deployed: say which before closing.
        setImported(result);
      } else {
        onImported(result);
        close();
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const canPreview = tab === 'github' ? Boolean(github.repo.trim()) : Boolean(files && files.length);
  const folderName = files && files.length
    ? (files[0].webkitRelativePath || '').split('/')[0] || 'folder'
    : null;

  return (
    <Dialog open={open} onClose={close} maxWidth="md" fullWidth>
      <DialogTitle>Import pipeline</DialogTitle>
      <DialogContent>
        <Tabs value={tab} onChange={(_, v) => { setTab(v); reset(); }} sx={{ mb: 2 }}>
          <Tab value="github" label="GitHub" />
          <Tab value="folder" label="Folder" />
        </Tabs>

        {tab === 'github' ? (
          <Stack spacing={2}>
            <TextField
              label="Repository" placeholder="apache/tooling-agents" size="small" required
              value={github.repo} onChange={(e) => { setGithub({ ...github, repo: e.target.value }); reset(); }}
            />
            <Stack direction="row" spacing={2}>
              <TextField
                label="Branch, tag or commit" size="small" sx={{ flex: 1 }}
                value={github.ref} onChange={(e) => { setGithub({ ...github, ref: e.target.value }); reset(); }}
              />
              <TextField
                label="Folder with pipeline.yaml" placeholder="ASVS" size="small" sx={{ flex: 1 }}
                value={github.path} onChange={(e) => { setGithub({ ...github, path: e.target.value }); reset(); }}
              />
            </Stack>
            <TextField
              label="Token (private repos)" type="password" size="small" autoComplete="off"
              helperText="Used for this import only; not stored."
              value={github.token} onChange={(e) => setGithub({ ...github, token: e.target.value })}
            />
          </Stack>
        ) : (
          <Stack spacing={1} alignItems="flex-start">
            <input
              ref={folderInput} type="file" hidden multiple
              webkitdirectory="" directory=""
              data-testid="folder-input"
              onChange={(e) => { setFiles(e.target.files); reset(); }}
            />
            <Button variant="outlined" startIcon={<FolderOpenIcon />} onClick={() => folderInput.current?.click()}>
              Choose folder
            </Button>
            <Typography variant="body2" color="text.secondary">
              {folderName
                ? `${folderName}: ${files.length} files`
                : 'Pick the folder that holds pipeline.yaml (or one above it).'}
            </Typography>
          </Stack>
        )}

        <FormControlLabel
          sx={{ mt: 2, display: 'flex' }}
          control={<Checkbox checked={labeled} onChange={(e) => { setLabeled(e.target.checked); reset(); }} />}
          label={<Typography variant="body2">Label this copy</Typography>}
        />
        {labeled ? (
          <TextField
            label="Label" size="small" sx={{ mt: 1, width: 260 }}
            placeholder={tab === 'github' ? 'commit hash' : 'timestamp'}
            helperText="Same label updates that copy; a new one makes another."
            value={label} onChange={(e) => { setLabel(e.target.value); reset(); }}
          />
        ) : (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
            Imports into the stable copy: the plain agent names, each at /rest/ its name. It updates in place.
          </Typography>
        )}

        <FormControlLabel
          sx={{ mt: 1, display: 'flex' }}
          control={<Checkbox checked={deploy} onChange={(e) => setDeploy(e.target.checked)} />}
          label={<Typography variant="body2">Deploy every agent (each at /rest/ its name and label)</Typography>}
        />

        {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}
        {imported && (
          <Alert severity="warning" sx={{ mt: 2 }}>
            Imported, and deployed {imported.deployed.length} of {Object.keys(imported.written).length} agents. Not deployed:
            {imported.deployProblems.map((p) => <div key={p.agent}><code>{p.agent}</code>: {p.reason}</div>)}
          </Alert>
        )}

        {plan && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              {plan.label ? `${plan.pipeline}@${plan.label}` : `${plan.pipeline} (stable)`}
              {plan.source?.commit && ` · ${plan.source.repo} ${plan.source.commit.slice(0, 7)}`}
            </Typography>
            {plan.problems.length > 0 && (
              <Alert severity="warning" sx={{ mb: 1 }}>
                {plan.problems.map((p) => <div key={p}>{p}</div>)}
              </Alert>
            )}
            <PlanTable plan={plan} />
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        {imported ? (
          <Button variant="contained" onClick={() => { onImported(imported); close(); }}>Done</Button>
        ) : (<>
        <Button onClick={close}>Cancel</Button>
        <Button onClick={() => send(true)} disabled={!canPreview || busy}>
          {busy && !plan ? <CircularProgress size={18} /> : 'Preview'}
        </Button>
        <Button
          variant="contained"
          onClick={() => send(false)}
          disabled={!plan || plan.problems.length > 0 || busy}
        >
          {busy && plan ? <CircularProgress size={18} color="inherit" /> : 'Import'}
        </Button>
        </>)}
      </DialogActions>
    </Dialog>
  );
}
