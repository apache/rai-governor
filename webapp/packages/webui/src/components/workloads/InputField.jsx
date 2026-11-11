// One workload input as a form field: a choice or listed repositories as a
// select, a repository as text checked against the project's naming, the
// rest as text.
import React from 'react';
import { MenuItem, TextField } from '@mui/material';

export default function InputField({ input, value, onChange, blankLabel, label, helperText }) {
  const common = {
    size: 'small', fullWidth: true, label: label || input.label, value: value ?? '',
    onChange: (e) => onChange(e.target.value), helperText: helperText ?? (input.help || undefined),
    sx: { bgcolor: '#fff' },
  };
  if (input.kind === 'choice' || (input.kind === 'repository' && input.choices?.length)) {
    return (
      <TextField select {...common} slotProps={{ select: { displayEmpty: Boolean(blankLabel) }, inputLabel: blankLabel ? { shrink: true } : undefined }}>
        {blankLabel && <MenuItem value=""><em>{blankLabel}</em></MenuItem>}
        {input.choices.map((c) => <MenuItem key={c} value={c}>{c}</MenuItem>)}
      </TextField>
    );
  }
  if (input.kind === 'repository') {
    return (
      <TextField
        {...common}
        placeholder={blankLabel || 'apache/…'}
        helperText={helperText ?? (input.help || 'One of this project\'s repositories: apache/<project> or apache/<project>-…')}
        slotProps={{ input: { sx: { fontFamily: 'monospace' } }, inputLabel: blankLabel ? { shrink: true } : undefined }}
      />
    );
  }
  return <TextField {...common} placeholder={blankLabel} slotProps={{ inputLabel: blankLabel ? { shrink: true } : undefined }} />;
}
