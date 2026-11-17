// One run (B11): what it was, how it went, its live log, and the memory
// entries it wrote, with links into the Memory page filtered to the run.
//
// Lives at /run/<id>: the API owns /runs. Any member of the run's project
// can open it; the agent page (site admins only) is never the link.
import React, { useEffect, useMemo, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Link,
  Paper,
  Typography,
} from '@mui/material';
import useRun from '../hooks/useRun';
import workloadService from '../services/workloadService';
import memoryService from '../services/memoryService';
import { RunStatus } from '../components/workloads/runBits';
import { duration, memoriesPath, runTarget, when } from '../components/workloads/runFormat';

const REUSABLE = ['convention', 'method'];

function Field({ label, children, wide }) {
  return (
    <Box sx={{ gridColumn: wide ? 'span 2' : 'auto', minWidth: 0 }}>
      <Typography variant="overline" sx={{ color: '#71717a', fontSize: '0.65rem', lineHeight: 1.6, fontWeight: 600 }}>{label}</Typography>
      <Typography component="div" variant="body2" sx={{ overflowWrap: 'anywhere' }}>{children}</Typography>
    </Box>
  );
}

function eventLine(ev) {
  const msg = ev.message || ev.error || ev.result_preview || '';
  if (ev.type === 'stdout' || ev.type === 'log') return msg;
  if (ev.type === 'trace_truncated') return `… ${msg} …`;
  if (ev.type === 'agent_start') return `▶ ${ev.agent_name}`;
  if (ev.type === 'agent_end') return `■ ${ev.agent_name}${ev.error ? `: ${ev.error}` : ''}`;
  if (ev.type === 'llm_call') return `>>> ${ev.provider || ''}/${ev.model || ''}`;
  return `${ev.type}${msg ? `: ${msg}` : ''}`;
}

function MemoriesPanel({ runId, finished }) {
  const [out, setOut] = useState(null);
  const [off, setOff] = useState(false);

  useEffect(() => {
    let live = true;
    memoryService.isEnabled().then((on) => {
      if (!on) { if (live) setOff(true); return; }
      memoryService.forRun(runId).then((o) => { if (live) setOut(o); }, () => { if (live) setOut({ domains: [] }); });
    });
    return () => { live = false; };
  }, [runId, finished]);

  if (off) return null;
  return (
    <Paper variant="outlined" sx={{ p: 2.5, borderColor: '#18181b' }} component="section" aria-label="Memories from this run">
      <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 600, mb: 1 }}>Memories from this run</Typography>
      {!out && <CircularProgress size={20} />}
      {out && out.domains.length === 0 && (
        <Typography variant="body2" color="text.secondary">Nothing written to memory you can read.</Typography>
      )}
      {out?.domains.map((d) => {
        const findings = d.by_type?.fact || 0;
        return (
          <Box key={d.domain} sx={{ display: 'flex', flexDirection: 'column', gap: 1.25, mb: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {d.entries} {d.entries === 1 ? 'entry' : 'entries'} in <code>{d.domain}</code>
              {d.entries !== d.live && ` (${d.entries - d.live} no longer live)`}
            </Typography>
            <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 0.5 }}>
              {Object.entries(d.by_type || {}).map(([t, n]) => (
                <Box component="li" key={t} sx={{ display: 'flex', justifyContent: 'space-between', fontSize: 14 }}>
                  <span>{t === 'fact' ? 'Findings and facts' : t}</span><span style={{ color: '#71717a' }}>{n}</span>
                </Box>
              ))}
            </Box>
            {findings > 0 && (
              <Button component={RouterLink} to={memoriesPath(d.domain, runId, null, 'triage')} variant="contained"
                sx={{ bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } }}>
                Triage findings
              </Button>
            )}
            <Button component={RouterLink} to={memoriesPath(d.domain, runId)} variant={findings > 0 ? 'outlined' : 'contained'}
              sx={findings > 0 ? { textTransform: 'none', color: '#18181b', borderColor: '#d4d4d8' }
                : { bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } }}>
              Review in Memory
            </Button>
            {d.canReview && findings > 0 && (
              <>
                <Button component={RouterLink} to={memoriesPath(d.domain, runId, 'findings')} variant="outlined"
                  sx={{ textTransform: 'none', color: '#18181b', borderColor: '#d4d4d8' }}>
                  Demote its findings…
                </Button>
                <Typography variant="caption" sx={{ color: '#71717a', lineHeight: 1.5 }}>
                  Selects the {findings} findings and leaves the {REUSABLE.join(' and ')} entries (security profile, domains, guidance) for reuse. Nothing changes until you confirm.
                </Typography>
              </>
            )}
          </Box>
        );
      })}
    </Paper>
  );
}

export default function RunPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState(null);
  const [workload, setWorkload] = useState(null);
  const [error, setError] = useState(null);
  const [stopMsg, setStopMsg] = useState(null);
  const { events, status, final, reconnect } = useRun(runId);

  // A different run: clear the page and load it.
  useEffect(() => {
    let live = true;
    setRun(null);
    setError(null);
    workloadService.getRun(runId).then((r) => { if (live) setRun(r); }, (e) => { if (live) setError(e.status === 404 ? 'not-found' : e.message); });
    return () => { live = false; };
  }, [runId]);

  // The run finished (or the stream replayed a finished run's end): load it
  // again for its final status, keeping the page as it is meanwhile. Clearing
  // it here made every run page show, go blank, and show again.
  useEffect(() => {
    if (!final) return undefined;
    let live = true;
    workloadService.getRun(runId).then((r) => { if (live) setRun(r); }, () => {});
    return () => { live = false; };
  }, [runId, final]);

  useEffect(() => {
    if (!run?.project || !run?.workloadId) return;
    workloadService.list(run.project).then(
      (out) => setWorkload(out.workloads.find((w) => w.id === run.workloadId) || null),
      () => {},
    );
  }, [run?.project, run?.workloadId]);

  const lines = useMemo(() => events.slice(-600).map(eventLine).filter(Boolean), [events]);

  if (error === 'not-found') {
    return <Box sx={{ p: 4, maxWidth: 720, mx: 'auto' }}><Alert severity="info">No such run, or not one you can see.</Alert></Box>;
  }
  if (error) return <Box sx={{ p: 4 }}><Alert severity="error">{error}</Alert></Box>;
  if (!run) return <Box sx={{ display: 'flex', justifyContent: 'center', p: 6 }}><CircularProgress size={28} /></Box>;

  const name = workload?.name || run.agentName;
  const failed = run.status === 'error' || run.status === 'stopped';
  const inputs = Object.entries(run.inputDict || {});

  const stop = async () => {
    setStopMsg(null);
    try {
      await workloadService.stopRun(runId);
      setStopMsg('Stop requested. The run stops at its next checkpoint.');
    } catch (e) {
      setStopMsg(e.message);
    }
  };

  const runAgain = () => navigate(`/projects/${encodeURIComponent(run.project)}/workloads`, {
    state: { prefill: { workloadId: run.workloadId, inputs: run.inputDict } },
  });

  return (
    <Box sx={{ p: { xs: 2, md: 4 }, maxWidth: 1360, mx: 'auto', display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      <Box component="nav" aria-label="Breadcrumb" sx={{ fontSize: 13, color: '#71717a', display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        {run.project ? (
          <>
            <Link component={RouterLink} to={`/projects/${encodeURIComponent(run.project)}/workloads`} sx={{ color: '#52525b' }}>Workloads</Link>
            <span aria-hidden>/</span>
            <Link component={RouterLink} to={`/projects/${encodeURIComponent(run.project)}/workloads`} sx={{ color: '#52525b' }}>{run.project}</Link>
            <span aria-hidden>/</span>
          </>
        ) : null}
        <span>{name}</span><span aria-hidden>/</span>
        <span style={{ fontFamily: 'monospace', color: '#18181b' }}>{runId.slice(0, 8)}</span>
      </Box>

      {failed && (
        <Alert severity={run.status === 'error' ? 'error' : 'warning'}
          action={run.workloadId && run.project ? <Button color="inherit" size="small" onClick={runAgain}>Run again</Button> : null}>
          {run.status === 'error' ? `Failed: ${run.error || 'see the log'}` : 'Stopped before it finished.'}
        </Alert>
      )}

      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2.5, alignItems: 'flex-start' }}>
        <Box sx={{ flex: '999 1 600px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2.5 }}>
          <Paper variant="outlined" sx={{ p: 3 }}>
            <Typography variant="caption" sx={{ fontFamily: 'monospace', color: '#71717a' }}>run {runId}</Typography>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 2, flexWrap: 'wrap', mb: 2 }}>
              <Typography variant="h5" component="h1" sx={{ fontWeight: 600 }}>
                {name}{runTarget(run.inputDict) ? ` of ${runTarget(run.inputDict)}` : ''}
              </Typography>
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                <RunStatus run={run} />
                {run.status === 'running' && (
                  <Button size="small" variant="outlined" color="error" onClick={stop} sx={{ textTransform: 'none' }}>Stop</Button>
                )}
                {!failed && run.status !== 'running' && run.workloadId && run.project && (
                  <Button size="small" variant="outlined" onClick={runAgain} sx={{ textTransform: 'none', color: '#18181b', borderColor: '#d4d4d8' }}>Run again</Button>
                )}
              </Box>
            </Box>
            {stopMsg && <Alert severity="info" sx={{ mb: 2 }}>{stopMsg}</Alert>}
            <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr 1fr', md: 'repeat(4, minmax(0, 1fr))' }, gap: 1.5, pt: 1.5, borderTop: '1px solid #f4f4f5' }}>
              <Field label="Project">{run.project || '—'}</Field>
              <Field label="Started by">{run.userId}</Field>
              <Field label="Started">{when(run.startedAt)}</Field>
              <Field label={run.status === 'running' ? 'Running for' : 'Took'}>{duration(run.startedAt, run.completedAt)}</Field>
              {inputs.map(([k, v]) => (
                <Field key={k} label={k}>
                  <span style={{ fontFamily: typeof v === 'string' && !v.startsWith('(') ? 'monospace' : 'inherit', color: typeof v === 'string' && v.startsWith('(') ? '#71717a' : 'inherit' }}>
                    {typeof v === 'string' ? v : JSON.stringify(v)}
                  </span>
                </Field>
              ))}
            </Box>
          </Paper>

          <Paper variant="outlined" sx={{ p: 3 }} component="section" aria-label="Progress log">
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 1, flexWrap: 'wrap', mb: 1 }}>
              <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 600 }}>Progress log</Typography>
              <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                <Typography variant="caption" color="text.secondary">
                  {status === 'running' ? 'live' : status === 'reconnecting' ? 'reconnecting…' : status}
                  {events.length > 600 && ` · the latest 600 of ${events.length} events`}
                </Typography>
                {status === 'disconnected' && <Button size="small" onClick={reconnect}>Reconnect</Button>}
              </Box>
            </Box>
            <Box component="pre" sx={{ m: 0, bgcolor: '#fafafa', border: '1px solid #f4f4f5', borderRadius: 1, p: 1.5, fontSize: 12, lineHeight: 1.65, maxHeight: 520, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {lines.length ? lines.join('\n') : 'Waiting for the first events…'}
            </Box>
          </Paper>

          {final?.result && (
            <Paper variant="outlined" sx={{ p: 3 }} component="section" aria-label="Result">
              <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 600, mb: 1 }}>Result</Typography>
              <Box component="pre" sx={{ m: 0, fontSize: 12, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {typeof final.result === 'string' ? final.result : JSON.stringify(final.result, null, 2)}
              </Box>
            </Paper>
          )}
        </Box>

        <Box component="aside" sx={{ flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <MemoriesPanel runId={runId} finished={run.status} />
        </Box>
      </Box>
    </Box>
  );
}
