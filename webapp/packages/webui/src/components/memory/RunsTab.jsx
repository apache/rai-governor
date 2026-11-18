// Runs: the runs with entries in the domains you read, newest first, each
// domain with what its triage needs. Site admins see every run; others the
// runs they started and their projects' runs.
import React, { useEffect, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import { Alert, Box, Chip, CircularProgress, Link, Paper, Stack, ToggleButton, Typography } from '@mui/material';
import memoryService from '../../services/memoryService';
import { shortDate } from './memoryFormat';

const STATUS = { running: 'running', success: 'finished', error: 'failed', stopped: 'stopped' };
const chipSx = { height: 20, fontSize: '0.7rem', bgcolor: '#f4f4f5', border: '1px solid #e4e4e7' };
const needSx = { ...chipSx, bgcolor: '#fef9c3', color: '#854d0e', border: '1px solid #fde68a' };

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const memLink = (domain, run, tab) => `/memories/${encodeURIComponent(domain)}?run=${encodeURIComponent(run)}&tab=${tab}`;

function DomainLine({ t, run }) {
  const triage = t.findings > 0 || t.proposals > 0;
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75 }}>
      <Typography variant="body2" sx={{ fontWeight: 500, minWidth: 120 }}>{t.domain}</Typography>
      {t.repos.length > 0 && <Typography variant="caption" color="text.secondary">{t.repos.join(', ')}</Typography>}
      {t.needs > 0 && <Chip size="small" sx={needSx} label={`${t.needs} need a decision`} />}
      {t.decided > 0 && <Chip size="small" sx={chipSx} label={`${t.decided} decided`} />}
      {t.proposals > 0 && <Chip size="small" sx={chipSx} label={plural(t.proposals, 'proposal', 'proposals')} />}
      {t.seen > 0 && <Chip size="small" sx={chipSx} label={`found ${t.seen} again`} />}
      <Chip size="small" sx={chipSx} label={plural(t.entries, 'entry written', 'entries written')} />
      <Box sx={{ display: 'flex', gap: 1.5, ml: 'auto' }}>
        {triage && (
          <Link component={RouterLink} to={memLink(t.domain, run, 'triage')} variant="body2" sx={{ color: '#18181b', fontWeight: 500 }}>
            Triage
          </Link>
        )}
        <Link component={RouterLink} to={memLink(t.domain, run, 'browse')} variant="body2" sx={{ color: '#18181b' }}>
          Browse
        </Link>
      </Box>
    </Box>
  );
}

export default function RunsTab() {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let live = true;
    memoryService.allRuns().then((o) => { if (live) setRuns(o.runs); }, (e) => { if (live) { setError(e.message); setRuns([]); } });
    return () => { live = false; };
  }, []);

  if (runs === null) return <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>;
  const needing = (r) => r.domains.some((t) => t.needs > 0 || t.proposals > 0);
  const shown = open ? runs.filter(needing) : runs;

  return (
    <Box>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 1, mb: 1.5, flexWrap: 'wrap' }}>
        <Typography variant="caption" color="text.secondary">
          {plural(shown.length, 'run', 'runs')} · newest first
        </Typography>
        <ToggleButton
          size="small" value="open" selected={open} onChange={() => setOpen(!open)}
          sx={{ textTransform: 'none', bgcolor: '#fff' }}
        >
          Needs a decision
        </ToggleButton>
      </Box>
      {shown.length === 0 && !error && (
        <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
          {open ? 'Nothing waiting for a decision.' : 'No runs have written to memory you can read.'}
        </Typography>
      )}
      <Stack spacing={1}>
        {shown.map((r) => {
          const i = r.info || {};
          return (
            <Paper key={r.run} variant="outlined" sx={{ p: 1.5 }} component="section" aria-label={`Run ${r.run}`}>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: 1, mb: 1 }}>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {i.visible ? i.agentName : `Run ${r.run.slice(0, 8)}`}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {[i.project, i.visible && (STATUS[i.status] || i.status), i.userId, shortDate(i.startedAt || r.last)]
                    .filter(Boolean).join(' · ')}
                </Typography>
                {i.visible && (
                  <Link component={RouterLink} to={`/run/${encodeURIComponent(r.run)}`} variant="caption" sx={{ ml: 'auto', color: '#52525b' }}>
                    Open run
                  </Link>
                )}
              </Box>
              <Stack spacing={0.75}>
                {r.domains.map((t) => <DomainLine key={t.domain} t={t} run={r.run} />)}
              </Stack>
            </Paper>
          );
        })}
      </Stack>
    </Box>
  );
}
