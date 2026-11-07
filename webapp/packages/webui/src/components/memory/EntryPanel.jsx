// One entry read in full: provenance on top, then the body.
import React, { useEffect, useState } from 'react';
import { Alert, Box, CircularProgress, Paper, Typography } from '@mui/material';
import memoryService from '../../services/memoryService';
import { EntryBody, SourceLink, TrustDisc } from './memoryBits';
import { TIER_TEXT, shortDate } from './memoryFormat';

function Field({ label, children }) {
  return (
    <Box>
      <Typography variant="overline" sx={{ color: 'text.secondary', lineHeight: 1.6, fontSize: '0.65rem' }}>
        {label}
      </Typography>
      <Typography component="div" variant="body2">{children}</Typography>
    </Box>
  );
}

// narrow: two columns, for a sidebar.
export function Provenance({ front, narrow = false }) {
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: narrow ? '1fr 1fr' : { xs: '1fr 1fr', sm: '1fr 1fr 1fr' }, gap: 1.5 }}>
      <Field label="Trust">
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
          <TrustDisc tier={front.trust_tier} />
          {TIER_TEXT[front.trust_tier] || front.trust_tier}
          {front.status && ` · ${front.status}`}
        </Box>
      </Field>
      <Field label="Type">
        {front.type}{front.confidence && ` · confidence ${front.confidence}`}
      </Field>
      <Field label="Review by">{front.review_by ? shortDate(front.review_by) : '—'}</Field>
      <Field label="Author"><code>{front.author}</code></Field>
      <Field label="Written">{shortDate(front.created)}</Field>
      {front.supersedes && <Field label="Supersedes"><code>{front.supersedes}</code></Field>}
      {front.source?.length > 0 && (
        <Box sx={{ gridColumn: '1 / -1' }}>
          <Field label="From">
            {front.source.map((s) => <div key={s}><SourceLink source={s} /></div>)}
          </Field>
        </Box>
      )}
    </Box>
  );
}

// actions(entry frontmatter, history or null) renders the buttons below the body.
export default function EntryPanel({ id, actions }) {
  const [entry, setEntry] = useState(null);
  const [history, setHistory] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setEntry(null);
    setHistory(null);
    setError(null);
    memoryService.get(id).then(
      (e) => { if (live) setEntry(e); },
      (e) => { if (live) setError(e.message); },
    );
    memoryService.history(id).then(
      (h) => { if (live) setHistory(h); },
      () => {},
    );
    return () => { live = false; };
  }, [id]);

  return (
    <Paper variant="outlined" sx={{ p: 3 }}>
      {error && <Alert severity="error">{error}</Alert>}
      {!entry && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={24} /></Box>}
      {entry && (
        <>
          <Typography variant="caption" sx={{ fontFamily: 'monospace', color: 'text.secondary' }}>{id}</Typography>
          <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
            {entry.frontmatter.title || entry.frontmatter.summary || id}
          </Typography>
          <Provenance front={entry.frontmatter} />
          <Box sx={{ borderTop: '1px solid #e4e4e7', my: 2 }} />
          <EntryBody body={entry.body} />
          {actions && <Box sx={{ mt: 2 }}>{actions(entry.frontmatter, history)}</Box>}
        </>
      )}
    </Paper>
  );
}
