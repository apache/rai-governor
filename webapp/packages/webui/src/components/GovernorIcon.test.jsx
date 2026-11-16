import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import GovernorIcon, { GOVERNOR_ASPECT } from './GovernorIcon';

describe('GovernorIcon', () => {
  it('renders an accessible image with default props', () => {
    render(<GovernorIcon />);
    const icon = screen.getByRole('img', { name: 'Governor' });
    expect(icon).toBeInTheDocument();
    expect(icon.style.height).toBe('24px');
    expect(icon.style.width).toBe(`${Math.round(24 * GOVERNOR_ASPECT)}px`);
    expect(icon.style.backgroundColor).toBe('currentcolor');
  });

  it('scales width with the artwork aspect ratio', () => {
    render(<GovernorIcon size={48} />);
    const icon = screen.getByTestId('governor-icon');
    expect(icon.style.height).toBe('48px');
    expect(icon.style.width).toBe(`${Math.round(48 * GOVERNOR_ASPECT)}px`);
  });

  it('applies a custom colour', () => {
    render(<GovernorIcon color="#ff0000" />);
    expect(screen.getByTestId('governor-icon').style.backgroundColor).toBe('rgb(255, 0, 0)');
  });

  it('accepts white for dark backgrounds', () => {
    render(<GovernorIcon color="white" />);
    expect(screen.getByTestId('governor-icon').style.backgroundColor).toBe('white');
  });

  it('uses the symbol asset as its mask', () => {
    render(<GovernorIcon />);
    const style = screen.getByTestId('governor-icon').getAttribute('style');
    expect(style).toMatch(/governor-symbol/);
  });

  it('accepts a custom accessible title', () => {
    render(<GovernorIcon title="Home" />);
    expect(screen.getByRole('img', { name: 'Home' })).toBeInTheDocument();
  });
});
