import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProfileMenu from './ProfileMenu';

let auth = {};
vi.mock('../contexts/AuthContextValue', () => ({ useAuth: () => auth }));

const USER = { uid: 'asf:alice', displayName: 'alice', workspaces: [] };

const menu = () => {
  render(<MemoryRouter><ProfileMenu /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: /account of current user/i }));
};

describe('sign out everywhere', () => {
  beforeEach(() => {
    auth = { user: USER, isSessionAuth: true, logout: vi.fn(), logoutEverywhere: vi.fn(), refreshWorkspaces: vi.fn() };
  });

  it('sits next to logout and ends every session', async () => {
    menu();
    expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out everywhere' }));
    await waitFor(() => expect(auth.logoutEverywhere).toHaveBeenCalledTimes(1));
    expect(auth.logout).not.toHaveBeenCalled();
  });

  it('a failure shows', async () => {
    auth.logoutEverywhere.mockRejectedValue(new Error('Sign out everywhere failed: 500'));
    menu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out everywhere' }));
    expect(await screen.findByText('Sign out everywhere failed: 500')).toBeInTheDocument();
  });

  it('is not offered without session sign-in', () => {
    auth = { user: USER, isSessionAuth: false, logout: vi.fn() };
    menu();
    expect(screen.queryByRole('menuitem', { name: 'Sign out everywhere' })).toBeNull();
  });
});
