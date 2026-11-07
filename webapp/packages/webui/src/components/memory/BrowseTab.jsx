// Browse: search a domain's summaries, read one entry beside the list.
import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  InputAdornment,
  MenuItem,
  Paper,
  Stack,
  TextField,
  ToggleButton,
  Typography,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import memoryService from '../../services/memoryService';
import { EntryMetaLine } from './memoryBits';
import EntryPanel from './EntryPanel';
import { today } from './memoryFormat';

const TYPES = ['decision', 'fact', 'convention', 'method', 'handoff', 'policy'];
const TIERS = ['raw', 'curated', 'verified'];

export function HitCard({ hit, selected, onClick, extra }) {
  return (
    <Paper
      variant="outlined"
      onClick={onClick}
      sx={{
        p: 1.5,
        cursor: 'pointer',
        borderColor: selected ? '#18181b' : undefined,
        borderWidth: selected ? 1.5 : 1,
        '&:hover': { borderColor: '#71717a' },
      }}
    >
      <EntryMetaLine hit={hit} extra={extra} />
      <Typography variant="body2" sx={{ fontWeight: 600, mt: 0.5 }}>{hit.title || hit.id}</Typography>
      {hit.summary && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>{hit.summary}</Typography>
      )}
    </Paper>
  );
}

export default function BrowseTab({ domain }) {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const [query, setQuery] = useState('');
  const [type, setType] = useState('');
  const [trustTier, setTrustTier] = useState('');
  const [due, setDue] = useState(false);
  const [inactive, setInactive] = useState(false);
  const [hits, setHits] = useState(null);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const out = await memoryService.search(domain, {
        q: query, type, trustTier, dueBy: due ? today() : undefined, includeInactive: inactive,
      });
      setHits(out.hits);
      setSelected((cur) => (out.hits.some((h) => h.id === cur) ? cur : out.hits[0]?.id || null));
    } catch (e) {
      setError(e.message);
      setHits([]);
    }
  }, [domain, query, type, trustTier, due, inactive]);

  useEffect(() => { load(); }, [load]);

  return (
    <Box>
      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1} sx={{ mb: 2 }}>
        <TextField
          size="small" fullWidth placeholder="Search titles and summaries"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') setQuery(q.trim()); }}
          onBlur={() => setQuery(q.trim())}
          slotProps={{ input: { startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment> } }}
          sx={{ bgcolor: '#fff' }}
        />
        <TextField
          select size="small" label="Type" value={type} onChange={(e) => setType(e.target.value)}
          sx={{ minWidth: 130, bgcolor: '#fff' }}
        >
          <MenuItem value="">any</MenuItem>
          {TYPES.map((t) => <MenuItem key={t} value={t}>{t}</MenuItem>)}
        </TextField>
        <TextField
          select size="small" label="Trust" value={trustTier} onChange={(e) => setTrustTier(e.target.value)}
          sx={{ minWidth: 130, bgcolor: '#fff' }}
        >
          <MenuItem value="">any</MenuItem>
          {TIERS.map((t) => <MenuItem key={t} value={t}>{t}</MenuItem>)}
        </TextField>
        <ToggleButton
          size="small" value="due" selected={due} onChange={() => setDue(!due)}
          sx={{ whiteSpace: 'nowrap', textTransform: 'none', bgcolor: '#fff' }}
        >
          Due for review
        </ToggleButton>
        <ToggleButton
          size="small" value="inactive" selected={inactive} onChange={() => setInactive(!inactive)}
          sx={{ whiteSpace: 'nowrap', textTransform: 'none', bgcolor: '#fff' }}
        >
          Show superseded
        </ToggleButton>
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {hits === null ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>
      ) : (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '5fr 7fr' }, gap: 2, alignItems: 'start' }}>
          <Stack spacing={1}>
            <Typography variant="caption" color="text.secondary">
              {hits.length} {hits.length === 1 ? 'entry' : 'entries'} · summaries only; open one to read it
            </Typography>
            {hits.length === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                {query ? 'Nothing matches.' : 'Nothing recorded in this domain yet.'}
              </Typography>
            )}
            {hits.map((hit) => (
              <HitCard key={hit.id} hit={hit} selected={hit.id === selected} onClick={() => setSelected(hit.id)} />
            ))}
          </Stack>
          {selected && (
            <EntryPanel
              id={selected}
              actions={(entry, history) => (
                <Stack direction="row" spacing={1}>
                  <Button variant="contained" size="small" onClick={() => navigate(`/memories/entries/${encodeURIComponent(entry.id)}`)}>
                    Open full entry
                  </Button>
                  {history && (
                    <Button variant="outlined" size="small" onClick={() => navigate(`/memories/entries/${encodeURIComponent(entry.id)}#history`)}>
                      History · {history.versions.length} {history.versions.length === 1 ? 'version' : 'versions'}
                    </Button>
                  )}
                </Stack>
              )}
            />
          )}
        </Box>
      )}
    </Box>
  );
}
