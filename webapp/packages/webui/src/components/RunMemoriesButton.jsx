// "Memories from this run" for a sandbox or deployment run (B11): Memory,
// filtered to what the run wrote. Asks which domains the run wrote to only
// when clicked, so a list of runs costs nothing until one is opened. One
// domain opens it; several offer a menu; none says so. Hidden when memory
// is off.
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { useNavigate } from 'react-router-dom';
import { Button, IconButton, ListItemText, Menu, MenuItem, Tooltip, Typography } from '@mui/material';
import PsychologyOutlinedIcon from '@mui/icons-material/PsychologyOutlined';
import memoryService from '../services/memoryService';
import { memoriesPath } from './workloads/runFormat';

const NOTHING = 'Nothing from this run in memory you can read';
const NOTHING_SHORT = 'Nothing in memory';

export default function RunMemoriesButton({ runId, compact }) {
  const navigate = useNavigate();
  const [on, setOn] = useState(false);
  const [anchor, setAnchor] = useState(null);
  const [domains, setDomains] = useState(null);
  const [note, setNote] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    memoryService.isEnabled().then((v) => { if (live) setOn(v); });
    return () => { live = false; };
  }, []);

  // A different run starts over.
  useEffect(() => { setDomains(null); setNote(null); setAnchor(null); }, [runId]);

  if (!on || !runId) return null;

  const open = async (e) => {
    e.stopPropagation();
    const at = e.currentTarget;
    setBusy(true);
    try {
      const out = await memoryService.forRun(runId);
      const ds = out.domains || [];
      if (ds.length === 1) {
        navigate(memoriesPath(ds[0].domain, runId));
      } else if (ds.length === 0) {
        setNote(NOTHING);
      } else {
        setDomains(ds);
        setAnchor(at);
      }
    } catch (err) {
      setNote(err.message || NOTHING);
    } finally {
      setBusy(false);
    }
  };

  const trigger = compact ? (
    <IconButton size="small" aria-label="Memories from this run" onClick={open} disabled={busy}>
      <PsychologyOutlinedIcon sx={{ fontSize: 18 }} />
    </IconButton>
  ) : (
    <Button size="small" variant="outlined" onClick={open} disabled={busy}
      startIcon={<PsychologyOutlinedIcon />}
      sx={{ textTransform: 'none', color: '#18181b', borderColor: '#d4d4d8' }}>
      Memories from this run
    </Button>
  );

  return (
    <>
      {compact ? <Tooltip title="Memories from this run" arrow>{trigger}</Tooltip> : trigger}
      {note && (
        <Typography variant="caption" role="status" title={note}
          onClick={(e) => e.stopPropagation()}
          sx={{ color: '#71717a', whiteSpace: 'nowrap' }}>
          {compact && note === NOTHING ? NOTHING_SHORT : note}
        </Typography>
      )}
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}
        onClick={(e) => e.stopPropagation()}>
        {(domains || []).map((d) => (
          <MenuItem key={d.domain} onClick={() => { setAnchor(null); navigate(memoriesPath(d.domain, runId)); }}>
            <ListItemText primary={d.domain} secondary={`${d.entries} ${d.entries === 1 ? 'entry' : 'entries'}`} />
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

RunMemoriesButton.propTypes = {
  runId: PropTypes.string,
  compact: PropTypes.bool,
};
