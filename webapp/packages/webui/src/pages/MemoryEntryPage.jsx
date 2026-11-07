// One memory entry in full, with its provenance and history (ISSUE-026).
import React, { useEffect, useState } from 'react';
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Box,
  Breadcrumbs,
  Button,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Link,
  Paper,
  Stack,
  Typography,
} from '@mui/material';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import memoryService from '../services/memoryService';
import { Provenance } from '../components/memory/EntryPanel';
import { EntryBody, TrustDisc } from '../components/memory/memoryBits';
import { shortDate } from '../components/memory/memoryFormat';

const ACTION_TEXT = {
  keep: 'kept',
  curate: 'curated',
  verify: 'verified',
  demote: 'demoted',
};

// Reviews and replaced versions, newest first, as one list.
function History({ history, onOpenVersion }) {
  const items = [
    ...history.reviews.map((r) => ({ kind: 'review', at: r.at, r })),
    ...history.versions.map((v) => ({ kind: 'version', at: v.replaced, v })),
  ].sort((a, b) => (b.at || '').localeCompare(a.at || ''));
  if (items.length === 0) {
    return <Typography variant="body2" color="text.secondary">Written once; never reviewed or changed.</Typography>;
  }
  return (
    <Stack spacing={1.5}>
      {items.map((item) => (item.kind === 'review' ? (
        <Box key={`r-${item.at}`}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
            {item.r.action !== 'demote' && <TrustDisc tier={item.r.tier} />}
            <Typography variant="body2">
              <b>{ACTION_TEXT[item.r.action] || item.r.action}</b> by <code>{item.r.by}</code>
            </Typography>
          </Box>
          <Typography variant="caption" color="text.secondary">
            {shortDate(item.r.at)}{item.r.review_by && ` · next review ${shortDate(item.r.review_by)}`}
          </Typography>
          {item.r.why && <Typography variant="body2" sx={{ mt: 0.25 }}>{item.r.why}</Typography>}
        </Box>
      ) : (
        <Box key={`v-${item.v.version}`}>
          <Typography variant="body2">
            Version replaced{' '}
            <Link component="button" type="button" onClick={() => onOpenVersion(item.v.version)}>
              {item.v.digest}
            </Link>
          </Typography>
          <Typography variant="caption" color="text.secondary">{shortDate(item.v.replaced)}</Typography>
        </Box>
      )))}
    </Stack>
  );
}

export default function MemoryEntryPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const [entry, setEntry] = useState(null);
  const [history, setHistory] = useState(null);
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let live = true;
    setEntry(null);
    setError(null);
    memoryService.get(id).then((e) => { if (live) setEntry(e); }, (e) => { if (live) setError(e.message); });
    memoryService.history(id).then((h) => { if (live) setHistory(h); }, () => {});
    return () => { live = false; };
  }, [id]);

  useEffect(() => {
    if (history && location.hash === '#history') {
      document.getElementById('history')?.scrollIntoView?.();
    }
  }, [history, location.hash]);

  const openVersion = async (name) => {
    try {
      setVersion(await memoryService.version(id, name));
    } catch (e) {
      setError(e.message);
    }
  };

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
    } catch {
      // No clipboard access: the id is on the page to select.
    }
  };

  const front = entry?.frontmatter;

  return (
    <Box sx={{ p: { xs: 2, md: 4 }, maxWidth: 1200, mx: 'auto' }}>
      <Breadcrumbs sx={{ mb: 2 }}>
        <Link component={RouterLink} to="/memories" underline="hover" color="inherit">Memory</Link>
        {front && (
          <Link component={RouterLink} to={`/memories/${encodeURIComponent(front.domain)}`} underline="hover" color="inherit">
            {front.domain}
          </Link>
        )}
        <Typography color="text.primary" sx={{ fontFamily: 'monospace', fontSize: '0.85rem' }}>{id}</Typography>
      </Breadcrumbs>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
      {!entry && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>}

      {front && (
        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '8fr 4fr' }, gap: 2, alignItems: 'start' }}>
          <Paper variant="outlined" sx={{ p: 3 }}>
            <Typography variant="h5" sx={{ fontWeight: 600, mb: 0.5 }}>{front.title || front.summary || id}</Typography>
            {front.title && front.summary && (
              <Typography variant="body1" color="text.secondary" sx={{ mb: 2 }}>{front.summary}</Typography>
            )}
            {front.status && (
              <Alert severity="warning" sx={{ mb: 2 }}>
                This entry is {front.status}. It is kept for the record and left out of search and recall.
              </Alert>
            )}
            <EntryBody body={entry.body} />
          </Paper>
          <Stack spacing={2}>
            <Paper variant="outlined" sx={{ p: 2 }}>
              <Provenance front={front} narrow />
              <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
                {!front.status && (
                  <Button
                    variant="contained" size="small"
                    onClick={() => navigate(`/memories/${encodeURIComponent(front.domain)}?tab=review&entry=${encodeURIComponent(id)}`)}
                  >
                    Review now
                  </Button>
                )}
                <Button variant="outlined" size="small" startIcon={<ContentCopyIcon />} onClick={copyId}>
                  {copied ? 'Copied' : 'Copy id'}
                </Button>
              </Stack>
            </Paper>
            <Paper variant="outlined" sx={{ p: 2 }} id="history">
              <Typography variant="subtitle2" sx={{ mb: 1 }}>History</Typography>
              {history ? <History history={history} onOpenVersion={openVersion} /> : <CircularProgress size={18} />}
            </Paper>
          </Stack>
        </Box>
      )}

      <Dialog open={Boolean(version)} onClose={() => setVersion(null)} maxWidth="md" fullWidth>
        {version && (
          <>
            <DialogTitle>
              Earlier version{' '}
              <Typography component="span" sx={{ fontFamily: 'monospace', fontSize: '0.85rem', color: 'text.secondary' }}>
                {version.version}
              </Typography>
            </DialogTitle>
            <DialogContent>
              <Provenance front={version.frontmatter} />
              <Box sx={{ borderTop: '1px solid #e4e4e7', my: 2 }} />
              <EntryBody body={version.body} />
            </DialogContent>
          </>
        )}
      </Dialog>
    </Box>
  );
}
