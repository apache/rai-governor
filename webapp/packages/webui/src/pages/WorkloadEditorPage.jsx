// Publish a deployed agent as a workload, or change one (site admins, B11).
//
// For each input the agent takes, say who sets it: project members (with a
// label, a kind, a default and, for text, the form it must take), a site
// admin (a fixed value members never see), or nobody (the agent's own
// default). Then which projects may add it to their Workloads page.
//
// /workload-catalog/new?deployment=<name> and /workload-catalog/<id>.
import React, { useEffect, useMemo, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  FormControlLabel,
  IconButton,
  InputAdornment,
  Link,
  MenuItem,
  Paper,
  Radio,
  RadioGroup,
  Switch,
  TextField,
  Typography,
} from '@mui/material';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import workloadService from '../services/workloadService';
import { bodyFrom, humanize, isSecretName, repositoriesText, rowsFor } from '../components/workloads/publishForm';

const ROLES = [
  { value: 'member', label: 'Members set it' },
  { value: 'fixed', label: 'A site admin fixes it' },
  { value: 'unused', label: 'Not used (the agent\'s default)' },
];
const KINDS = [
  { value: 'text', label: 'Text' },
  { value: 'repository', label: 'Repository (the project\'s own)' },
  { value: 'choice', label: 'Choice' },
];
const FIELD = { bgcolor: '#fff' };
const DARK = { bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } };

function SecretField({ label, value, onChange }) {
  const [show, setShow] = useState(false);
  return (
    <TextField
      size="small" fullWidth label={label} value={value} onChange={(e) => onChange(e.target.value)}
      type={show ? 'text' : 'password'} autoComplete="off" sx={FIELD}
      slotProps={{ input: { endAdornment: (
        <InputAdornment position="end">
          <IconButton size="small" aria-label={show ? 'Hide value' : 'Show value'} onClick={() => setShow(!show)}>
            {show ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
          </IconButton>
        </InputAdornment>
      ) } }}
    />
  );
}

function InputRow({ row, onChange }) {
  const set = (k) => (v) => onChange({ ...row, [k]: v });
  return (
    <Box sx={{ py: 2, borderTop: '1px solid #f4f4f5', display: 'grid', gridTemplateColumns: { xs: '1fr', md: '220px 1fr' }, gap: 2 }}>
      <Box>
        <Typography variant="body2" sx={{ fontFamily: 'monospace', fontWeight: 600 }}>{row.name}</Typography>
        <TextField select size="small" fullWidth value={row.role} onChange={(e) => set('role')(e.target.value)}
          sx={{ mt: 1, ...FIELD }} slotProps={{ htmlInput: { 'aria-label': `Who sets ${row.name}` } }}>
          {ROLES.map((r) => <MenuItem key={r.value} value={r.value}>{r.label}</MenuItem>)}
        </TextField>
      </Box>
      <Box>
        {row.role === 'member' && (
          <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5 }}>
            <TextField size="small" label="Label" value={row.label} onChange={(e) => set('label')(e.target.value)} sx={FIELD} />
            <TextField select size="small" label="Kind" value={row.kind} onChange={(e) => set('kind')(e.target.value)} sx={FIELD}>
              {KINDS.map((k) => <MenuItem key={k.value} value={k.value}>{k.label}</MenuItem>)}
            </TextField>
            {row.kind === 'choice' && (
              <TextField size="small" label="Choices, comma-separated" value={row.choicesText}
                onChange={(e) => set('choicesText')(e.target.value)} sx={{ gridColumn: '1 / -1', ...FIELD }} />
            )}
            <TextField size="small" label="Default" value={row.default} onChange={(e) => set('default')(e.target.value)}
              helperText={row.kind === 'repository' ? 'Blank: the first listed for the project' : ' '} sx={FIELD} />
            <TextField size="small" label="Help (shown under the field)" value={row.help} onChange={(e) => set('help')(e.target.value)} sx={FIELD} helperText=" " />
            {row.kind === 'text' && (
              <TextField size="small" label="Must match (regex, whole value)" value={row.pattern}
                onChange={(e) => set('pattern')(e.target.value)} sx={{ gridColumn: '1 / -1', ...FIELD }}
                helperText="Blank: letters, digits and . _ / + @ : = - only, no .. or //. Agents build URLs and config lines from it with your tokens."
                slotProps={{ input: { sx: { fontFamily: 'monospace' } } }} />
            )}
            <FormControlLabel control={<Checkbox size="small" checked={row.required} onChange={(e) => set('required')(e.target.checked)} />}
              label={<Typography variant="body2">Required</Typography>} />
          </Box>
        )}
        {row.role === 'fixed' && (isSecretName(row.name)
          ? <SecretField label="Value (members never see it)" value={row.value} onChange={set('value')} />
          : <TextField size="small" fullWidth label="Value (members see only that it's set)" value={row.value}
              onChange={(e) => set('value')(e.target.value)} sx={FIELD}
              helperText={row.json ? 'JSON: sent to the agent as a number, list or object, not text' : undefined} />)}
        {row.role === 'fixed' && isSecretName(row.name) && (
          <Typography variant="caption" sx={{ color: '#71717a' }}>Scrubbed from every run's log, result and error.</Typography>
        )}
        {row.role === 'unused' && (
          <Typography variant="body2" sx={{ color: '#71717a', pt: 1 }}>Not sent: the agent uses its own default.</Typography>
        )}
      </Box>
    </Box>
  );
}

const EMPTY = {
  deployment: '', name: '', description: '', produces: '', typicalDuration: '', memoryNote: '', enabled: true,
  scope: 'some', projectsText: '', reposText: '', rows: [],
};

export default function WorkloadEditorPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [deployments, setDeployments] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let live = true;
    // The same page serves /new and every /:id, so start each from nothing:
    // never carry one workload's fixed values into another.
    setForm(EMPTY);
    setLoaded(false);
    setError(null);
    (async () => {
      try {
        const deps = await workloadService.deployments();
        if (!live) return;
        setDeployments(deps);
        if (id) {
          const all = await workloadService.catalog();
          const w = all.workloads.find((x) => x.id === id);
          if (!w) { setError('No such workload in the catalog.'); return; }
          const dep = deps.find((d) => d.friendlyName === w.deployment);
          setForm({
            deployment: w.deployment, name: w.name, description: w.description || '', produces: w.produces || '',
            typicalDuration: w.typicalDuration || '', memoryNote: w.memoryNote || '', enabled: w.enabled !== false,
            scope: (w.projects || []).includes('*') ? 'all' : 'some',
            projectsText: (w.projects || []).filter((p) => p !== '*').join(', '),
            reposText: repositoriesText(w.repositories),
            rows: rowsFor(Object.keys(dep?.inputSchema || {}), w),
          });
        } else {
          const chosen = params.get('deployment') || '';
          const dep = deps.find((d) => d.friendlyName === chosen);
          if (dep) {
            setForm({ ...EMPTY, deployment: dep.friendlyName, name: humanize(dep.friendlyName),
              description: dep.description || '', rows: rowsFor(Object.keys(dep.inputSchema || {})) });
          }
        }
        setLoaded(true);
      } catch (e) {
        if (live) setError(e.status === 403 ? 'Publishing workloads is for site administrators.' : e.message);
      }
    })();
    return () => { live = false; };
  }, [id, params]);

  const pickDeployment = (name) => {
    const dep = deployments.find((d) => d.friendlyName === name);
    setForm((f) => ({
      ...f, deployment: name, name: f.name || humanize(name), description: f.description || dep?.description || '',
      rows: rowsFor(Object.keys(dep?.inputSchema || {})),
    }));
  };

  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }));
  const counts = useMemo(() => ({
    member: form.rows.filter((r) => r.role === 'member').length,
    fixed: form.rows.filter((r) => r.role === 'fixed').length,
  }), [form.rows]);

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = bodyFrom(form);
      if (id) await workloadService.update(id, body);
      else await workloadService.publish(body);
      navigate('/workload-catalog');
    } catch (err) {
      setError(err.message);
      setSaving(false);
    }
  };

  if (error && !loaded) return <Box sx={{ p: 4, maxWidth: 720, mx: 'auto' }}><Alert severity="error">{error}</Alert></Box>;
  if (!deployments) return <Box sx={{ display: 'flex', justifyContent: 'center', p: 6 }}><CircularProgress size={28} /></Box>;

  return (
    <Box component="form" onSubmit={save} sx={{ p: { xs: 2, md: 4 }, maxWidth: 1100, mx: 'auto', display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      <Box component="nav" aria-label="Breadcrumb" sx={{ fontSize: 13, color: '#71717a', display: 'flex', gap: 1 }}>
        <Link component={RouterLink} to="/workload-catalog" sx={{ color: '#52525b' }}>Workload catalog</Link>
        <span aria-hidden>/</span><span>{id ? form.name || id : 'Publish a workload'}</span>
      </Box>
      <Box>
        <Typography variant="h5" component="h1" sx={{ fontWeight: 600 }}>{id ? `Change ${form.name || 'workload'}` : 'Publish a workload'}</Typography>
        <Typography variant="body2" color="text.secondary">
          A deployed agent, offered to projects. Their PMC adds it to their Workloads page; their members run it.
        </Typography>
      </Box>

      <Paper variant="outlined" sx={{ p: 3, display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 2 }}>
        <TextField select size="small" label="Deployed agent" value={form.deployment} required
          onChange={(e) => pickDeployment(e.target.value)} sx={FIELD}
          helperText={form.deployment ? `Runs POST /rest/${form.deployment}` : 'Deploy an agent first to offer it here'}>
          {deployments.map((d) => <MenuItem key={d.friendlyName} value={d.friendlyName}>{d.friendlyName}</MenuItem>)}
        </TextField>
        <TextField size="small" label="Name" value={form.name} onChange={(e) => set('name')(e.target.value)} required sx={FIELD} helperText=" " />
        <TextField size="small" label="What it does" value={form.description} onChange={(e) => set('description')(e.target.value)}
          multiline minRows={2} sx={{ gridColumn: '1 / -1', ...FIELD }} />
        <TextField size="small" label="What it produces" value={form.produces} onChange={(e) => set('produces')(e.target.value)}
          placeholder="Reports in apache/tooling-runbooks" sx={FIELD} />
        <TextField size="small" label="Typical run" value={form.typicalDuration} onChange={(e) => set('typicalDuration')(e.target.value)}
          placeholder="5 to 7 hours at L3" sx={FIELD} />
        <TextField size="small" label="Memory note (shown on each run)" value={form.memoryNote}
          onChange={(e) => set('memoryNote')(e.target.value)} inputProps={{ maxLength: 200 }}
          placeholder="Findings go to the project's domain" sx={{ gridColumn: '1 / -1', ...FIELD }} />
      </Paper>

      {form.deployment && (
        <Paper variant="outlined" sx={{ p: 3 }} component="section" aria-label="Inputs">
          <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 600 }}>Inputs</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {counts.member} set by members, {counts.fixed} fixed by you. Members never see fixed values; runs record only their names.
          </Typography>
          {form.rows.length === 0 && <Typography variant="body2" color="text.secondary">This agent declares no inputs.</Typography>}
          {form.rows.map((r, n) => (
            <InputRow key={r.name} row={r} onChange={(next) => setForm((f) => ({ ...f, rows: f.rows.map((x, k) => (k === n ? next : x)) }))} />
          ))}
        </Paper>
      )}

      <Paper variant="outlined" sx={{ p: 3, display: 'flex', flexDirection: 'column', gap: 2 }} component="section" aria-label="Who may use it">
        <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 600 }}>Who may add it</Typography>
        <RadioGroup value={form.scope} onChange={(e) => set('scope')(e.target.value)}>
          <FormControlLabel value="all" control={<Radio size="small" />} label="Every project" />
          <FormControlLabel value="some" control={<Radio size="small" />} label="These projects" />
        </RadioGroup>
        {form.scope === 'some' && (
          <TextField size="small" label="Projects (LDAP names, comma-separated)" value={form.projectsText}
            onChange={(e) => set('projectsText')(e.target.value)} placeholder="tooling, whimsy" sx={FIELD} />
        )}
        <TextField size="small" label="Repositories members may pick, per project (optional)" value={form.reposText}
          onChange={(e) => set('reposText')(e.target.value)} multiline minRows={3}
          placeholder={'tooling: apache/tooling-llmao, apache/tooling-agents\nwhimsy: apache/whimsy'}
          helperText="One line per project. A project with no line gets apache/<project> and apache/<project>-*, typed."
          slotProps={{ input: { sx: { fontFamily: 'monospace', fontSize: 13 } } }} sx={FIELD} />
        <FormControlLabel control={<Switch checked={form.enabled} onChange={(e) => set('enabled')(e.target.checked)} />}
          label="Enabled: projects can add and run it" />
      </Paper>

      {error && <Alert severity="error">{error}</Alert>}
      <Box sx={{ display: 'flex', gap: 1.5 }}>
        <Button type="submit" variant="contained" disabled={saving || !form.deployment} sx={DARK}>
          {saving ? <CircularProgress size={16} color="inherit" /> : id ? 'Save changes' : 'Publish to the catalog'}
        </Button>
        <Button component={RouterLink} to="/workload-catalog" sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>
      </Box>
    </Box>
  );
}
