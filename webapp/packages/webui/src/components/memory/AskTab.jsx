// Ask: a question answered from the domain's entries, with the entries cited.
import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, CircularProgress, Paper, Stack, TextField, Typography } from '@mui/material';
import memoryService from '../../services/memoryService';
import { HitCard } from './BrowseTab';

const NO_ANSWER = 'The entries do not answer this.';

// The brief cites entries as [mem-...]; each citation becomes a link.
export function BriefText({ brief, onCite }) {
  const parts = brief.split(/(\[mem-[a-z0-9-]+\])/g);
  return (
    <Typography component="div" variant="body1" sx={{ whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>
      {parts.map((part, i) => {
        const m = part.match(/^\[(mem-[a-z0-9-]+)\]$/);
        if (!m) return <React.Fragment key={i}>{part}</React.Fragment>;
        return (
          <Box
            key={i} component="button" type="button" onClick={() => onCite(m[1])}
            sx={{
              border: 0, bgcolor: '#f4f4f5', borderRadius: 1, px: 0.5, mx: 0.25, cursor: 'pointer',
              fontFamily: 'monospace', fontSize: '0.8rem', color: 'inherit',
            }}
          >
            {m[1]}
          </Box>
        );
      })}
    </Typography>
  );
}

export default function AskTab({ domain }) {
  const navigate = useNavigate();
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState(null);

  const open = (id) => navigate(`/memories/entries/${encodeURIComponent(id)}`);

  const ask = async () => {
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      setAnswer(await memoryService.recall(domain, question.trim()));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const unreviewed = (answer?.entries || []).filter((e) => e.trust_tier === 'raw');
  // The model ends with "Unreviewed: <ids>"; the warning below says that.
  const brief = (answer?.brief || '').replace(/\n*Unreviewed:[^\n]*\s*$/, '');

  return (
    <Box>
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <TextField
          fullWidth size="small" placeholder="Ask what the record says, e.g. what is known about the order report query?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') ask(); }}
          sx={{ bgcolor: '#fff' }}
        />
        <Button variant="contained" onClick={ask} disabled={busy || !question.trim()}>
          {busy ? <CircularProgress size={18} color="inherit" /> : 'Ask'}
        </Button>
      </Stack>

      {error && <Alert severity="error">{error}</Alert>}

      {answer && answer.matched === 0 && (
        <Paper variant="outlined" sx={{ p: 3 }}>
          <Typography variant="body1" sx={{ fontWeight: 600 }}>Nothing in {domain} matches that.</Typography>
          <Typography variant="body2" color="text.secondary">
            No model was called. Try other words, or browse the domain.
          </Typography>
        </Paper>
      )}

      {answer && answer.matched > 0 && (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '7fr 5fr' }, gap: 2, alignItems: 'start' }}>
          <Paper variant="outlined" sx={{ p: 3 }}>
            <Typography variant="overline" color="text.secondary">Brief</Typography>
            {brief.trim() === NO_ANSWER ? (
              <Typography variant="body1">The entries found don't answer this. They are listed alongside.</Typography>
            ) : (
              <BriefText brief={brief} onCite={open} />
            )}
            {unreviewed.length > 0 && (
              <Alert severity="warning" sx={{ mt: 2 }}>
                Drawn partly from unreviewed entries: {unreviewed.map((e) => e.id).join(', ')}. Check them before acting.
              </Alert>
            )}
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
              Written by a model from {answer.cited.length} of {answer.matched} matching entries.
            </Typography>
          </Paper>
          <Stack spacing={1}>
            <Typography variant="caption" color="text.secondary">Entries given to the model</Typography>
            {(answer.entries || []).map((hit) => (
              <HitCard key={hit.id} hit={hit} onClick={() => open(hit.id)} />
            ))}
          </Stack>
        </Box>
      )}
    </Box>
  );
}
