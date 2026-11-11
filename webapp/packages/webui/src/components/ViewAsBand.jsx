// While a site admin views the site as a regular user, a band under the top
// bar says so on every page, with the way back.
import React, { useContext, useState } from 'react';
import { Box, Button, Typography } from '@mui/material';
import VisibilityOutlinedIcon from '@mui/icons-material/VisibilityOutlined';
import { AuthContext } from '../contexts/AuthContextValue';
import { reloadApp, setViewAsUser } from '../services/viewAsService';

export default function ViewAsBand() {
  const auth = useContext(AuthContext);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  if (!auth?.user?.viewingAsUser) return null;

  const back = async () => {
    setBusy(true);
    setError(null);
    try {
      await setViewAsUser(false);
      reloadApp();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <Box role="status" sx={{
      position: 'sticky', top: 56, zIndex: 2, display: 'flex', alignItems: 'center', gap: 1.5,
      flexWrap: 'wrap', px: 3, py: 0.75, bgcolor: '#fef3c7', borderBottom: '1px solid #fcd34d', color: '#78350f',
    }}>
      <VisibilityOutlinedIcon sx={{ fontSize: 18 }} />
      <Typography variant="body2" sx={{ flex: '1 1 320px' }}>
        <strong>Viewing as a regular user.</strong> You have your LDAP project roles only: no site admin, no allowlists.
        {error && <span style={{ color: '#b91c1c' }}> {error}</span>}
      </Typography>
      <Button size="small" variant="contained" onClick={back} disabled={busy}
        sx={{ textTransform: 'none', bgcolor: '#78350f', '&:hover': { bgcolor: '#92400e' } }}>
        Back to admin
      </Button>
    </Box>
  );
}
