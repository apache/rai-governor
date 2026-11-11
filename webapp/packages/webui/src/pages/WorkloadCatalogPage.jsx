// The workload catalog (site admins, B11): what's published, to whom, and
// which projects added it. Publish, change, disable or unpublish from here.
import React, { useCallback, useEffect, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Link,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import workloadService from '../services/workloadService';

const DARK = { bgcolor: '#18181b', textTransform: 'none', '&:hover': { bgcolor: '#27272a' } };

export default function WorkloadCatalogPage() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [gone, setGone] = useState(null);

  const load = useCallback(() => {
    workloadService.catalog().then(
      (out) => setItems(out.workloads),
      (e) => setError(e.status === 403 ? 'The catalog is for site administrators.' : e.message),
    );
  }, []);
  useEffect(() => { load(); }, [load]);

  const unpublish = async () => {
    try {
      await workloadService.unpublish(gone.id);
      setGone(null);
      load();
    } catch (e) {
      setError(e.message);
      setGone(null);
    }
  };

  return (
    <Box sx={{ p: { xs: 2, md: 4 }, maxWidth: 1360, mx: 'auto', display: 'flex', flexDirection: 'column', gap: 2.5 }}>
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box>
          <Typography variant="h5" component="h1" sx={{ fontWeight: 600 }}>Workload catalog</Typography>
          <Typography variant="body2" color="text.secondary">
            Deployed agents offered to projects. A project's PMC adds them to its Workloads page.
          </Typography>
        </Box>
        {items && <Button component={RouterLink} to="/workload-catalog/new" variant="contained" sx={DARK}>Publish a workload</Button>}
      </Box>
      {error && <Alert severity="error">{error}</Alert>}
      {!items && !error && <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}><CircularProgress size={28} /></Box>}
      {items && items.length === 0 && (
        <Alert severity="info">
          Nothing published yet. Pick a deployed agent with Publish a workload, or use Publish as workload on the Deployed APIs page.
        </Alert>
      )}
      {items && items.length > 0 && (
        <Paper variant="outlined" sx={{ overflowX: 'auto' }}>
          <Table size="small" sx={{ minWidth: 900 }}>
            <TableHead>
              <TableRow>
                <TableCell>Workload</TableCell>
                <TableCell>Runs</TableCell>
                <TableCell>Offered to</TableCell>
                <TableCell>On the page of</TableCell>
                <TableCell>Inputs</TableCell>
                <TableCell>Published</TableCell>
                <TableCell align="right"><span style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>Actions</span></TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((w) => (
                <TableRow key={w.id}>
                  <TableCell>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>{w.name}</Typography>
                    {!w.enabled && <Chip label="Disabled" size="small" sx={{ height: 18, fontSize: '0.68rem', mt: 0.5 }} />}
                  </TableCell>
                  <TableCell sx={{ fontFamily: 'monospace', fontSize: 13 }}>{w.deployment}</TableCell>
                  <TableCell>{(w.projects || []).includes('*') ? 'Every project' : (w.projects || []).join(', ')}</TableCell>
                  <TableCell sx={{ color: '#52525b' }}>{(w.addedTo || []).length ? w.addedTo.join(', ') : 'None yet'}</TableCell>
                  <TableCell sx={{ color: '#52525b' }}>
                    {(w.inputs || []).length} by members · {Object.keys(w.fixedInputs || {}).length} fixed
                  </TableCell>
                  <TableCell sx={{ color: '#52525b', whiteSpace: 'nowrap' }}>
                    {w.publishedBy}{w.publishedAt ? `, ${new Date(w.publishedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}` : ''}
                  </TableCell>
                  <TableCell align="right" sx={{ whiteSpace: 'nowrap' }}>
                    <Link component={RouterLink} to={`/workload-catalog/${encodeURIComponent(w.id)}`} sx={{ color: '#18181b', fontWeight: 500 }}>Change</Link>
                    <Box component="span" aria-hidden sx={{ color: '#d4d4d8', px: 1 }}>|</Box>
                    <Link component="button" type="button" onClick={() => setGone(w)} sx={{ color: '#b91c1c', fontWeight: 500, verticalAlign: 'baseline' }}>Unpublish</Link>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Paper>
      )}
      {gone && (
        <Dialog open onClose={() => setGone(null)} aria-labelledby="unpublish-title">
          <DialogTitle id="unpublish-title">Unpublish {gone.name}?</DialogTitle>
          <DialogContent>
            <Typography variant="body2">
              It leaves the catalog and every project's Workloads page{gone.addedTo?.length ? ` (${gone.addedTo.join(', ')})` : ''}. Runs already made, their results and their memory stay. To pause it instead, change it and switch it off.
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setGone(null)} sx={{ textTransform: 'none', color: '#52525b' }}>Cancel</Button>
            <Button variant="contained" color="error" onClick={unpublish} sx={{ textTransform: 'none' }}>Unpublish</Button>
          </DialogActions>
        </Dialog>
      )}
    </Box>
  );
}
