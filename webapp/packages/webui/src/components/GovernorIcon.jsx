// webapp/packages/webui/src/components/GovernorIcon.jsx
import React from 'react';
import PropTypes from 'prop-types';
import governorSymbolUrl from '../assets/governor-symbol.svg';

// Width / height of the traced symbol's viewBox (1013 x 1242).
export const GOVERNOR_ASPECT = 1013 / 1242;

/**
 * Centrifugal-governor symbol for Apache Governor.
 *
 * The artwork is a detailed engraving (~115 KB of path data), so rather than
 * inlining it into the JS bundle it is loaded once as a static asset and used
 * as a CSS mask. That keeps it recolourable like an inline SVG: pass
 * color="#ffffff" on dark backgrounds and color="#18181b" on light ones.
 *
 * `size` is the rendered height in px; width follows the artwork's aspect ratio.
 */
const GovernorIcon = ({ size = 24, color = 'currentColor', title = 'Governor' }) => {
  const mask = `url("${governorSymbolUrl}") center / contain no-repeat`;
  return (
    <span
      role="img"
      aria-label={title}
      data-testid="governor-icon"
      style={{
        display: 'inline-block',
        flexShrink: 0,
        height: size,
        width: Math.round(size * GOVERNOR_ASPECT),
        backgroundColor: color,
        WebkitMask: mask,
        mask,
      }}
    />
  );
};

GovernorIcon.propTypes = {
  size: PropTypes.number,
  color: PropTypes.string,
  title: PropTypes.string,
};

export default GovernorIcon;
