// Memory (ISSUE-026): browse, ask and review one domain's entries.
//
// Lives at /memories, not /memory: the API owns /memory and nginx and the
// vite proxy send everything under it to the API.
import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Alert, Box, Chip, CircularProgress, MenuItem, Tab, Tabs, TextField, Typography } from '@mui/material';
import memoryService from '../services/memoryService';
import BrowseTab from '../components/memory/BrowseTab';
import AskTab from '../components/memory/AskTab';
import ReviewTab from '../components/memory/ReviewTab';

const TABS = ['browse', 'ask', 'review'];

export default function MemoryPage() {
  const navigate = useNavigate();
  const { domain: routeDomain } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = TABS.includes(params.get('tab')) ? params.get('tab') : 'browse';
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
              <MenuItem key={d.domain} value={d.domain}>{d.domain} · {d.entries} entries</MenuItem>
            ))}
          </TextField>
        )}
      </Box>

      {error && <Alert severity="error">{error}</Alert>}
      {!info && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>}
      {info && !current && (
        <Alert severity="warning">No memory domain {domain}. This deployment has: {domains.map((d) => d.domain).join(', ')}.</Alert>
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
          </Tabs>
          {tab === 'browse' && <BrowseTab key={domain} domain={domain} />}
          {tab === 'ask' && <AskTab key={domain} domain={domain} />}
          {tab === 'review' && (
            <ReviewTab
              key={domain} domain={domain} you={info.you}
              initialEntry={params.get('entry')} onReviewed={loadDomains}
            />
          )}
        </>
      )}
    </Box>
  );
}
