// Workloads (B11): what a project's members can run, with each workload's
// runs listed under it. Each run links to its own page and to the memory
// entries it wrote.
//
// Lives at /projects/<name>/workloads: the API owns /workloads.
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Link,
  MenuItem,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import workloadService from '../services/workloadService';
import memoryService from '../services/memoryService';
import { RunStatus } from '../components/workloads/runBits';
import { memoriesPath, runPath, runTarget, when } from '../components/workloads/runFormat';

const SHOWN = 5;

function InputField({ input, value, onChange }) {
  const common = {
    size: 'small', fullWidth: true, label: input.label, value: value ?? '',
    onChange: (e) => onChange(e.target.value), helperText: input.help || undefined,
    sx: { bgcolor: '#fff' },
  };
  if (input.kind === 'choice' || (input.kind === 'repository' && input.choices?.length)) {
    return (
      <TextField select {...common}>
        {input.choices.map((c) => <MenuItem key={c} value={c}>{c}</MenuItem>)}
      </TextField>
    );
  }
  if (input.kind === 'repository') {
    return (
      <TextField
        {...common}
        placeholder="apache/…"
        helperText={input.help || 'One of this project\'s repositories: apache/<project> or apache/<project>-…'}
        slotProps={{ input: { sx: { fontFamily: 'monospace' } } }}
      />
    );
  }
  return <TextField {...common} />;
}

function defaultsOf(w, prefill) {
  const v = {};
  for (const i of w.inputs) v[i.name] = i.default ?? '';
  if (prefill?.workloadId === w.id) {
    for (const [k, val] of Object.entries(prefill.inputs || {})) {
      if (k in v && typeof val === 'string' && !val.startsWith('(')) v[k] = val;
    }
  }
  return v;
}

function WorkloadModule({ workload, project, runs, written, prefill }) {
  const navigate = useNavigate();
  const [values, setValues] = useState(() => defaultsOf(workload, prefill));
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState(null);
  const [all, setAll] = useState(false);

  const start = async (e) => {
    e.preventDefault();
    setStarting(true);
    setError(null);
    try {
      const out = await workloadService.run(workload.id, project, values);
      navigate(runPath(out.runId));
    } catch (err) {
      setError(err.message);
      setStarting(false);
    }
  };

  const shown = all ? runs : runs.slice(0, SHOWN);

  return (
    <Paper variant="outlined" component="section" aria-label={workload.name}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 3, p: 3, borderBottom: '1px solid #f4f4f5' }}>
        <Box sx={{ flex: '1 1 340px', minWidth: 0 }}>
          <Typography variant="h6" component="h2" sx={{ fontWeight: 600, mb: 0.5 }}>{workload.name}</Typography>
          {workload.description && (
            <Typography variant="body2" sx={{ color: '#3f3f46', lineHeight: 1.6 }}>{workload.description}</Typography>
          )}
          <Box component="dl" sx={{ m: 0, mt: 1.5, display: 'flex', flexWrap: 'wrap', columnGap: 2.5, rowGap: 0.5, fontSize: 13, color: '#52525b' }}>
            {workload.produces && <Box sx={{ display: 'flex', gap: 0.75 }}><dt style={{ fontWeight: 500 }}>Produces</dt><dd style={{ margin: 0 }}>{workload.produces}</dd></Box>}
            <Box sx={{ display: 'flex', gap: 0.75 }}><dt style={{ fontWeight: 500 }}>Memory</dt><dd style={{ margin: 0, fontFamily: 'monospace' }}>{workload.memoryNote || project}</dd></Box>
            {workload.typicalDuration && <Box sx={{ display: 'flex', gap: 0.75 }}><dt style={{ fontWeight: 500 }}>Typical run</dt><dd style={{ margin: 0 }}>{workload.typicalDuration}</dd></Box>}
          </Box>
        </Box>

        <Box component="form" onSubmit={start} sx={{ flex: '1 1 440px', minWidth: 0, display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5, alignContent: 'start' }}>
          {workload.inputs.map((i) => (
            <Box key={i.name} sx={{ gridColumn: i.kind === 'repository' ? '1 / -1' : 'auto' }}>
              <InputField input={i} value={values[i.name]} onChange={(v) => setValues((cur) => ({ ...cur, [i.name]: v }))} />
            </Box>
          ))}
          {workload.fixedInputNames?.length > 0 && (
            <Typography variant="caption" sx={{ color: '#71717a', gridColumn: '1 / -1' }}>
              Set by a site admin: {workload.fixedInputNames.join(', ')}.
            </Typography>
          )}
          {error && <Alert severity="error" sx={{ gridColumn: '1 / -1' }}>{error}</Alert>}
          <Box sx={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
            <Button type="submit" variant="contained" disabled={starting} startIcon={starting ? <CircularProgress size={14} color="inherit" /> : <PlayArrowIcon />}
              sx={{ bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } }}>
              Run for {project}
            </Button>
            <Typography variant="caption" sx={{ color: '#71717a' }}>Runs on the server; you can close this page.</Typography>
          </Box>
        </Box>
      </Box>

      <Box sx={{ px: 3, pb: 1 }}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', pt: 1.5, pb: 0.5 }}>
          <Typography variant="overline" component="h3" sx={{ color: '#52525b', fontWeight: 600 }}>Recent runs</Typography>
          {runs.length > SHOWN && (
            <Link component="button" type="button" variant="body2" onClick={() => setAll(!all)} sx={{ color: '#18181b' }}>
              {all ? 'Fewer' : `All ${runs.length} runs`}
            </Link>
          )}
        </Box>
        {runs.length === 0 ? (
          <Typography variant="body2" sx={{ color: '#71717a', pb: 2 }}>No runs yet for {project}.</Typography>
        ) : (
          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small" sx={{ minWidth: 820 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Status</TableCell>
                  <TableCell>Target</TableCell>
                  <TableCell>Started</TableCell>
                  <TableCell>By</TableCell>
                  <TableCell>Wrote</TableCell>
                  <TableCell align="right"><span style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>Links</span></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {shown.map((r) => {
                  const n = written[r.runId];
                  return (
                    <TableRow key={r.runId}>
                      <TableCell><RunStatus run={r} /></TableCell>
                      <TableCell sx={{ fontFamily: 'monospace', fontSize: 13 }}>{runTarget(r.inputDict)}</TableCell>
                      <TableCell sx={{ color: '#52525b', whiteSpace: 'nowrap' }}>{when(r.startedAt)}</TableCell>
                      <TableCell sx={{ color: '#52525b' }}>{r.userId}</TableCell>
                      <TableCell sx={{ color: '#52525b' }}>{n === undefined ? '—' : `${n} ${n === 1 ? 'entry' : 'entries'}`}</TableCell>
                      <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                        <Link component={RouterLink} to={runPath(r.runId)} sx={{ color: '#18181b', fontWeight: 500 }}>Open run</Link>
                        {n > 0 && (
                          <>
                            <Box component="span" aria-hidden sx={{ color: '#d4d4d8', px: 1 }}>|</Box>
                            <Link component={RouterLink} to={memoriesPath(project, r.runId)} sx={{ color: '#18181b', fontWeight: 500 }}>Memories ({n})</Link>
                          </>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </Box>
        )}
      </Box>
    </Paper>
  );
}

export default function WorkloadsPage() {
  const { project } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [projects, setProjects] = useState(null);
  const [data, setData] = useState(null);
  const [runs, setRuns] = useState([]);
  const [written, setWritten] = useState({});
  const [error, setError] = useState(null);

  useEffect(() => {
    workloadService.projects().then((out) => setProjects(out.projects), (e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!project && projects?.length) navigate(`/projects/${encodeURIComponent(projects[0].project)}/workloads`, { replace: true });
  }, [project, projects, navigate]);

  const load = useCallback(async () => {
    if (!project) return;
    setError(null);
    setData(null);
    try {
      const [w, r] = await Promise.all([
        workloadService.list(project),
        workloadService.runs(project, { limit: 300 }),
      ]);
      setData(w);
      setRuns(r.runs || []);
    } catch (e) {
      setError(e.message);
      return;
    }
    // Entry counts per run come from the project's memory domain; memory
    // may be off, or the domain not there yet.
    if (await memoryService.isEnabled()) {
      // As many runs as the page lists, or older ones lose their count.
      memoryService.runs(project, 200).then(
        (out) => setWritten(Object.fromEntries((out.runs || []).map((x) => [x.run, x.entries]))),
        () => setWritten({}),
      );
    }
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const byWorkload = useMemo(() => {
    const out = {};
    for (const r of runs) if (r.workloadId) (out[r.workloadId] ||= []).push(r);
    return out;
  }, [runs]);

  const role = projects?.find((p) => p.project === project)?.role;

  return (
    <Box sx={{ p: { xs: 2, md: 4 }, maxWidth: 1360, mx: 'auto' }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap', mb: 3 }}>
        <Box>
          <Typography variant="h5" component="h1" sx={{ fontWeight: 600 }}>Workloads</Typography>
          <Typography variant="body2" color="text.secondary">
            Scans and checks published for your project. Runs, their results and their memories belong to the project.
          </Typography>
        </Box>
        {projects?.length > 0 && (
          <TextField
            select size="small" label="Project" value={project || ''}
            onChange={(e) => navigate(`/projects/${encodeURIComponent(e.target.value)}/workloads`)}
            sx={{ minWidth: 240, bgcolor: '#fff' }}
          >
            {projects.map((p) => (
              <MenuItem key={p.project} value={p.project}>{p.project} · {p.role === 'admin' ? 'PMC' : 'committer'}</MenuItem>
            ))}
          </TextField>
        )}
      </Box>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {projects && projects.length === 0 && (
        <Alert severity="info">You aren't a member of any project with workloads yet.</Alert>
      )}
      {project && !data && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>}
      {data && data.workloads.length === 0 && (
        <Alert severity="info">No workloads are published for {project} yet. A site admin publishes them.</Alert>
      )}
      {data && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          {data.workloads.map((w) => (
            <WorkloadModule
              key={`${project}:${w.id}`} workload={w} project={project}
              runs={byWorkload[w.id] || []} written={written}
              prefill={location.state?.prefill}
            />
          ))}
          {role === 'admin' && (
            <Typography variant="caption" sx={{ color: '#71717a' }}>
              As {project}'s PMC you can stop any of these runs, and review the project's memory.
            </Typography>
          )}
        </Box>
      )}
    </Box>
  );
}
