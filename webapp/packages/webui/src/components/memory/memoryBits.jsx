// Small pieces the memory screens share: the trust disc, the meta line, dates.
import React from 'react';
import { Box, Chip, Typography } from '@mui/material';
import { isOverdue, shortDate } from './memoryFormat';

const TIER_FILL = { raw: 0, curated: 50, verified: 100 };

// Empty, half or full: how far an entry has been checked.
export function TrustDisc({ tier, size = 10 }) {
  const fill = TIER_FILL[tier] ?? 0;
  return (
    <Box
      component="span"
      role="img"
      aria-label={`trust: ${tier}`}
      sx={{
        display: 'inline-block',
        width: size,
        height: size,
        borderRadius: '50%',
        border: '1.5px solid #18181b',
        background: `linear-gradient(90deg, #18181b ${fill}%, transparent ${fill}%)`,
        flexShrink: 0,
      }}
    />
  );
}

// "raw · fact · Nov 3, 2026" with an overdue or status chip after it.
export function EntryMetaLine({ hit, extra }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
      <TrustDisc tier={hit.trust_tier} />
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {hit.trust_tier} · {hit.type} · {shortDate(hit.created)}
      </Typography>
      {hit.status && (
        <Chip label={hit.status} size="small" sx={{ height: 18, fontSize: '0.68rem' }} />
      )}
      {!hit.status && isOverdue(hit) && (
        <Chip
          label={`Review overdue · ${shortDate(hit.review_by)}`}
          size="small"
          sx={{ height: 18, fontSize: '0.68rem', bgcolor: '#fef9c3', color: '#854d0e' }}
        />
      )}
      {extra}
    </Box>
  );
}

// An entry body. The pilot has no Markdown renderer, so it shows the text
// as written; headings and code spans read fine as plain text.
export function EntryBody({ body }) {
  return (
    <Typography
      component="div"
      variant="body2"
      sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.6 }}
    >
      {body}
    </Typography>
  );
}

// A source is a URL or a note. URLs link out.
export function SourceLink({ source }) {
  if (/^https?:\/\//.test(source)) {
    return (
      <a href={source} target="_blank" rel="noreferrer" style={{ wordBreak: 'break-all', color: 'inherit' }}>
        {source.replace(/^https?:\/\//, '')}
      </a>
    );
  }
  return <span style={{ wordBreak: 'break-all' }}>{source}</span>;
}
