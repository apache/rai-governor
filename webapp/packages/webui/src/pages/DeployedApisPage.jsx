// webapp/packages/webui/src/pages/DeployedApisPage.jsx
import React, { useState, useEffect } from 'react';
import { Link as RouterLink } from 'react-router-dom';
import {
  Box,
  Typography,
  Paper,
  CircularProgress,
  Alert,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Divider,
  Container,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Button,
} from '@mui/material';
import ApiIcon from '@mui/icons-material/Api';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import agentService from '../services/agentService';
import workloadService from '../services/workloadService';

const DeployedApisPage = () => {
  const [apis, setApis] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [siteAdmin, setSiteAdmin] = useState(false);

  useEffect(() => {
    // Publishing is for site admins; others don't get the button.
    workloadService.projects().then((out) => setSiteAdmin(Boolean(out.isSiteAdmin)), () => setSiteAdmin(false));
  }, []);

  useEffect(() => {
    const fetchApis = async () => {
      try {
        setLoading(true);
        setError(null);
        const deployedApis = await agentService.getDeployments();
        setApis(deployedApis);
      } catch (err) {
        setError('Failed to load deployed APIs.');
        console.error(err);
      } finally {
        setLoading(false);
      }
    };

    fetchApis();
  }, []);

  return (
    <Container maxWidth="lg">
      <Typography variant="h4" component="h1" gutterBottom>
        Deployed APIs
      </Typography>
      
      <Paper sx={{ p: 3 }}>
        {loading && <Box sx={{ display: 'flex', justifyContent: 'center' }}><CircularProgress /></Box>}
        {error && <Alert severity="error">{error}</Alert>}
        {!loading && !error && (
          <List>
            {apis.length === 0 ? (
              <Typography color="text.secondary" textAlign="center">
                No agents are currently deployed as APIs.
              </Typography>
            ) : (
              apis.map((api) => (
                <Accordion key={api.agentId}>
                  <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                    <ListItemIcon sx={{mr: 2}}><ApiIcon /></ListItemIcon>
                    <ListItemText 
                      primary={api.friendlyName} 
                      secondary={api.description}
                    />
                  </AccordionSummary>
                  <AccordionDetails>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
                      <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                        Endpoint: POST /rest/{api.friendlyName}
                      </Typography>
                      {/* Offer it to projects as a workload (B11). */}
                      {siteAdmin && (
                      <Button
                        component={RouterLink} size="small" variant="outlined"
                        to={`/workload-catalog/new?deployment=${encodeURIComponent(api.friendlyName)}`}
                        sx={{ textTransform: 'none' }}
                      >
                        Publish as workload
                      </Button>
                      )}
                    </Box>
                    <Typography variant="subtitle2" sx={{mt: 2, mb: 1}}>Input Schema:</Typography>
                    <Paper variant="outlined" sx={{p: 1, backgroundColor: 'action.hover'}}>
                      <pre style={{margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all'}}>
                        {JSON.stringify(api.inputSchema, null, 2)}
                      </pre>
                    </Paper>
                    <Typography variant="subtitle2" sx={{mt: 2, mb: 1}}>Output Schema:</Typography>
                    <Paper variant="outlined" sx={{p: 1, backgroundColor: 'action.hover'}}>
                       <pre style={{margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all'}}>
                         {JSON.stringify(api.outputSchema, null, 2)}
                       </pre>
                    </Paper>
                  </AccordionDetails>
                </Accordion>
              ))
            )}
          </List>
        )}
      </Paper>
    </Container>
  );
};

export default DeployedApisPage;