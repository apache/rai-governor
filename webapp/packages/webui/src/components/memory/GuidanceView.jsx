// Guidance for a repo: what every scan of it is given from this domain,
// besides the repo's own AGENTS.md and SECURITY.md (scans that remember).
// A Browse filter. Proposals are listed too, marked as not given to scans
// until someone accepts them in triage.
import React, { useEffect, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Alert, Box, Chip, CircularProgress, Paper, Table, TableBody, TableCell, TableHead, TableRow, ToggleButton,
  ToggleButtonGroup, Typography,
} from '@mui/material';
import memoryService from '../../services/memoryService';
import { DigestPanel } from './TriageTab';
import { shortDate } from './memoryFormat';

const KIND = { policy: 'Always use', convention: 'context', decision: 'decision' };
const SCOPE = { all: 'All scanners', asvs: 'ASVS', glasswing: 'Glasswing' };

function from(sources) {
  if (!sources?.length) return '—';
  const mem = sources.filter((s) => s.startsWith('mem-'));
  const other = sources.filter((s) => !s.startsWith('mem-'));
  const parts = [];
  if (mem.length) parts.push(`${mem.length} ${mem.length === 1 ? 'entry' : 'entries'}`);
  if (other.length) parts.push(other[0].replace(/^https?:\/\/(www\.)?github\.com\//, ''));
  return parts.join(', ');
}

export default function GuidanceView({ domain, repo, canReview }) {
  const [scanner, setScanner] = useState('');
  const [out, setOut] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setOut(null);
    memoryService.guidance(domain, repo, scanner || undefined).then((o) => { if (live) setOut(o); }, (e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [domain, repo, scanner]);

  return (
    <Box>
      <DigestPanel domain={domain} repo={repo} canReview={canReview} />
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', mb: 1.5 }}>
        <ToggleButtonGroup size="small" exclusive value={scanner} onChange={(_, v) => setScanner(v ?? '')} aria-label="Scanner">
          <ToggleButton value="" sx={{ textTransform: 'none' }}>All scanners</ToggleButton>
          <ToggleButton value="asvs" sx={{ textTransform: 'none' }}>ASVS</ToggleButton>
          <ToggleButton value="glasswing" sx={{ textTransform: 'none' }}>Glasswing</ToggleButton>
        </ToggleButtonGroup>
        <Typography variant="body2" color="text.secondary">
          What {scanner ? SCOPE[scanner] : 'every'} scan of {repo} is given, besides the repo&apos;s own AGENTS.md and SECURITY.md
        </Typography>
      </Box>
      {error && <Alert severity="error">{error}</Alert>}
      {!out && !error && <CircularProgress size={24} />}
      {out && (
        <Paper variant="outlined">
          <Table size="small" aria-label={`Guidance for ${repo}`}>
            <TableHead>
              <TableRow>
                {['Guidance', 'Scope', 'Kind', 'From', 'Written by', 'Cited', 'Review by'].map((h) => <TableCell key={h}>{h}</TableCell>)}
              </TableRow>
            </TableHead>
            <TableBody>
              {out.entries.length === 0 && out.proposals.length === 0 && (
                <TableRow><TableCell colSpan={7}><Typography variant="body2" color="text.secondary">No guidance for {repo} yet.</Typography></TableCell></TableRow>
              )}
              {out.entries.map((e) => {
                const u = out.usage?.[e.id] || {};
                return (
                  <TableRow key={e.id}>
                    <TableCell>
                      <RouterLink to={`/memories/entries/${encodeURIComponent(e.id)}`} style={{ color: 'inherit', fontWeight: 600 }}>{e.title || e.summary}</RouterLink>
                      {e.title && e.summary && <Typography variant="caption" component="div" color="text.secondary">{e.summary}</Typography>}
                    </TableCell>
                    <TableCell>{SCOPE[e.scope] || e.scope}</TableCell>
                    <TableCell>{e.always ? <Chip label="Always use" size="small" sx={{ height: 20, fontSize: '0.7rem' }} /> : KIND[e.type] || e.type}</TableCell>
                    <TableCell>{from(e.source)}</TableCell>
                    <TableCell><code>{e.author}</code></TableCell>
                    <TableCell>{u.loaded || u.cited ? `${u.loaded || 0} loaded, ${u.cited || 0} cited` : 'not yet'}</TableCell>
                    <TableCell>{e.review_by ? shortDate(e.review_by) : '—'}</TableCell>
                  </TableRow>
                );
              })}
              {out.proposals.map((p) => (
                <TableRow key={p.id} sx={{ bgcolor: '#fafafa' }}>
                  <TableCell>
                    <RouterLink to={`/memories/entries/${encodeURIComponent(p.id)}`} style={{ color: 'inherit' }}>{p.title || p.summary}</RouterLink>
                    <Typography variant="caption" component="div" color="text.secondary">Proposed; not given to scans until accepted in triage.</Typography>
                  </TableCell>
                  <TableCell>All scanners</TableCell>
                  <TableCell><Chip label="Proposed" size="small" sx={{ height: 20, fontSize: '0.7rem', bgcolor: '#eff6ff', color: '#1e40af' }} /></TableCell>
                  <TableCell>{p.run ? <RouterLink to={`/memories/${encodeURIComponent(domain)}?run=${encodeURIComponent(p.run)}&tab=triage`}>run {p.run.slice(0, 8)}</RouterLink> : '—'}</TableCell>
                  <TableCell><code>{(p.author || '').replace(/ \(model:.*\)$/, '')}</code></TableCell>
                  <TableCell>—</TableCell>
                  <TableCell>—</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Paper>
      )}
    </Box>
  );
}
