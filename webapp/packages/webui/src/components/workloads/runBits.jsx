// Pieces the Workloads and run pages share: a run's status, times and links.
import React from 'react';
import { Box, Typography } from '@mui/material';
import { duration } from './runFormat';

const STATUS = {
  running: { text: 'Running', color: '#1d4ed8' },
  success: { text: 'Finished', color: '#15803d' },
  error: { text: 'Failed', color: '#b91c1c' },
  stopped: { text: 'Stopped', color: '#52525b' },
};

function Icon({ status, color }) {
  const common = { width: 14, height: 14, viewBox: '0 0 16 16', fill: 'none', stroke: color, strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true };
  if (status === 'running') return <svg {...common}><circle cx="8" cy="8" r="5.8" strokeDasharray="3 2.2" /></svg>;
  if (status === 'success') return <svg {...common}><circle cx="8" cy="8" r="5.8" /><path d="M5.4 8.2l1.8 1.8 3.4-3.6" /></svg>;
  if (status === 'error') return <svg {...common}><circle cx="8" cy="8" r="5.8" /><path d="M8 4.8v3.8M8 11v.2" /></svg>;
  return <svg {...common}><circle cx="8" cy="8" r="5.8" /><path d="M6 6h4v4H6z" /></svg>;
}

export function RunStatus({ run }) {
  const s = STATUS[run.status] || { text: run.status, color: '#52525b' };
  const extra = run.status === 'running' ? ` · ${duration(run.startedAt)}` : '';
  return (
    <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.9, color: s.color, fontWeight: 500, whiteSpace: 'nowrap' }}>
      <Icon status={run.status} color={s.color} />
      <Typography component="span" variant="body2" sx={{ fontWeight: 500, color: 'inherit' }}>{s.text}{extra}</Typography>
    </Box>
  );
}
