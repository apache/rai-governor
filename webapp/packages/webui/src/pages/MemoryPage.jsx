// Memory (ISSUE-026): browse, ask and review one domain's entries.
//
// Lives at /memories, not /memory: the API owns /memory and nginx and the
// vite proxy send everything under it to the API.
import React, { useCallback, useEffect, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Alert, Box, Chip, CircularProgress, Link, MenuItem, Paper, Tab, Tabs, TextField, Typography } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import memoryService from '../services/memoryService';
import BrowseTab from '../components/memory/BrowseTab';
import AskTab from '../components/memory/AskTab';
import ReviewTab from '../components/memory/ReviewTab';
import TriageTab from '../components/memory/TriageTab';

const TABS = ['browse', 'ask', 'review', 'triage'];

// Which run the list is limited to, and a way back to it (B11).
function RunBand({ runId, domain, onClear }) {
  const [info, setInfo] = useState(null);
  useEffect(() => {
    let live = true;
    memoryService.forRun(runId).then((o) => { if (live) setInfo(o); }, () => { if (live) setInfo({ run: { runId, visible: false }, domains: [] }); });
    return () => { live = false; };
  }, [runId]);
  const run = info?.run;
  const here = info?.domains.find((d) => d.domain === domain);
  const status = { running: 'running', success: 'finished', error: 'failed', stopped: 'stopped' }[run?.status] || run?.status;
  return (
    <Paper variant="outlined" sx={{ px: 2, py: 1.25, mb: 2, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.5 }}>
      <Chip
        label={`Run ${runId.slice(0, 8)}`} onDelete={onClear} deleteIcon={<CloseIcon aria-label="Clear the run filter" />}
        sx={{ fontWeight: 600, bgcolor: '#f4f4f5', border: '1px solid #d4d4d8' }}
      />
      <Typography variant="body2" sx={{ flex: '1 1 360px', color: '#3f3f46' }}>
        {run?.visible ? `${run.agentName}${run.project ? ` for ${run.project}` : ''} · ${status}` : 'A run you can\'t open'}
        {here && ` · wrote ${here.entries} ${here.entries === 1 ? 'entry' : 'entries'} here`}
      </Typography>
      {run?.visible && (
        <Link component={RouterLink} to={`/run/${encodeURIComponent(runId)}`} variant="body2" sx={{ color: '#18181b', fontWeight: 500 }}>
          Open run on Workloads
        </Link>
      )}
    </Paper>
  );
}

export default function MemoryPage() {
  const navigate = useNavigate();
  const { domain: routeDomain } = useParams();
  const [params, setParams] = useSearchParams();
  const run = params.get('run') || null;
  // Triage is a run's: the findings one scan wrote (scans that remember).
  const tab = TABS.includes(params.get('tab')) && (params.get('tab') !== 'triage' || run) ? params.get('tab') : 'browse';
  const select = params.get('select') || null;
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);

  const loadDomains = useCallback(() => {
    memoryService.domains().then(setInfo, (e) => setError(e.status === 404 ? 'off' : e.message));
  }, []);

  useEffect(() => { loadDomains(); }, [loadDomains]);

  const domains = info?.domains || [];
  const domain = routeDomain || domains[0]?.domain;
  const current = domains.find((d) => d.domain === domain);

  const setTab = (next) => {
    const p = new URLSearchParams(params);
    p.set('tab', next);
    p.delete('entry');
    setParams(p);
  };

  if (error === 'off') {
    return (
      <Box sx={{ p: 4, maxWidth: 720, mx: 'auto' }}>
        <Alert severity="info">Memory is not enabled on this deployment.</Alert>
      </Box>
    );
  }

  return (
    <Box sx={{ p: { xs: 2, md: 4 }, maxWidth: 1200, mx: 'auto' }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap', mb: 2 }}>
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 600 }}>Memory</Typography>
          <Typography variant="body2" color="text.secondary">
            What agents and people have recorded, with where it came from and how far it has been checked.
          </Typography>
        </Box>
        {domains.length > 0 && (
          <TextField
            select size="small" label="Domain" value={domain || ''}
            onChange={(e) => navigate(`/memories/${encodeURIComponent(e.target.value)}?tab=${tab}`)}
            sx={{ minWidth: 260, bgcolor: '#fff' }}
          >
            {domains.map((d) => (
              <MenuItem key={d.domain} value={d.domain}>
                {d.domain}{d.kind === 'project' ? ' (project)' : ''} · {d.entries} entries
              </MenuItem>
            ))}
          </TextField>
        )}
      </Box>

      {error && <Alert severity="error">{error}</Alert>}
      {!info && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>}
      {info && domains.length === 0 && (
        <Alert severity="info">
          There is no memory you can read yet. Each project has its own, which its committers can read once a run or its PMC has written to it.
        </Alert>
      )}
      {info && domains.length > 0 && !current && (
        <Alert severity="warning">No memory domain {domain} that you can read. You can read: {domains.map((d) => d.domain).join(', ')}.</Alert>
      )}

      {current && (
        <>
          <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 2, borderBottom: '1px solid #e4e4e7' }}>
            <Tab value="browse" label="Browse" />
            <Tab value="ask" label="Ask" />
            <Tab
              value="review"
              label={(
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  Review
                  <Chip label={current.review} size="small" sx={{ height: 20, fontSize: '0.7rem' }} />
                </Box>
              )}
            />
            {run && <Tab value="triage" label="Triage" />}
          </Tabs>
          {run && (tab === 'browse' || tab === 'triage') && (
            <RunBand runId={run} domain={domain} onClear={() => {
              const p = new URLSearchParams(params);
              p.delete('run');
              p.delete('select');
              setParams(p);
            }} />
          )}
          {tab === 'browse' && (
            <BrowseTab
              key={`${domain}:${run || ''}`} domain={domain} run={run} select={select}
              canReview={current.canReview ?? info.you?.canReview} onReviewed={loadDomains}
            />
          )}
          {tab === 'triage' && run && (
            <TriageTab
              key={`${domain}:${run}`} domain={domain} run={run}
              canReview={current.canReview ?? info.you?.canReview} onReviewed={loadDomains}
            />
          )}
          {tab === 'ask' && <AskTab key={domain} domain={domain} />}
          {tab === 'review' && (
            <ReviewTab
              key={domain} domain={domain} kind={current.kind}
              you={{ ...info.you, canReview: current.canReview ?? info.you?.canReview }}
              initialEntry={params.get('entry')} onReviewed={loadDomains}
            />
          )}
        </>
      )}
    </Box>
  );
}
