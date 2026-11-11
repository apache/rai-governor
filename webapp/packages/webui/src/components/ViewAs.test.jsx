import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProfileMenu from './ProfileMenu';
import ViewAsBand from './ViewAsBand';
import { reloadApp, setViewAsUser } from '../services/viewAsService';

let mockUser = null;
vi.mock('../contexts/AuthContextValue', async () => {
  const React = await import('react');
  const AuthContext = React.createContext(null);
  return {
    AuthContext,
    useAuth: () => ({ user: mockUser, isSessionAuth: true, logout: vi.fn(), refreshWorkspaces: vi.fn() }),
  };
});
vi.mock('../services/viewAsService', () => ({ setViewAsUser: vi.fn(), reloadApp: vi.fn() }));

const ADMIN = { uid: 'asf:akm', displayName: 'akm', workspaces: [], isSiteAdmin: true, canViewAsUser: true, viewingAsUser: false };
const VIEWING = { ...ADMIN, isSiteAdmin: false, viewingAsUser: true };

const menu = async (user) => {
  mockUser = user;
  render(<MemoryRouter><ProfileMenu /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: /account of current user/i }));
};

const band = async (user) => {
  const { AuthContext } = await import('../contexts/AuthContextValue');
  render(<AuthContext.Provider value={{ user }}><ViewAsBand /></AuthContext.Provider>);
};

describe('view as a regular user', () => {
  beforeEach(() => { vi.clearAllMocks(); setViewAsUser.mockResolvedValue({}); });

  it('a site admin switches it on from the profile menu, and the app reloads', async () => {
    await menu(ADMIN);
    const item = screen.getByRole('menuitemcheckbox', { name: 'View as a regular user' });
    expect(item).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(item);
    await waitFor(() => expect(setViewAsUser).toHaveBeenCalledWith(true));
    expect(reloadApp).toHaveBeenCalled();
  });

  it('while on, the menu says so and switches it off', async () => {
    await menu(VIEWING);
    expect(screen.getByText('Viewing as a regular user')).toBeInTheDocument();
    expect(screen.queryByText('Site admin')).toBeNull();
    const item = screen.getByRole('menuitemcheckbox', { name: 'View as a regular user' });
    expect(item).toHaveAttribute('aria-checked', 'true');
    fireEvent.click(item);
    await waitFor(() => expect(setViewAsUser).toHaveBeenCalledWith(false));
  });

  it('is not offered to anyone else', async () => {
    await menu({ uid: 'asf:alice', displayName: 'alice', workspaces: [], isSiteAdmin: false, canViewAsUser: false });
    expect(screen.queryByRole('menuitemcheckbox', { name: /view as a regular user/i })).toBeNull();
  });

  it('a refusal shows, and nothing reloads', async () => {
    setViewAsUser.mockRejectedValue(new Error('Only site admins can view as a regular user'));
    await menu(ADMIN);
    fireEvent.click(screen.getByRole('menuitemcheckbox', { name: 'View as a regular user' }));
    expect(await screen.findByText('Only site admins can view as a regular user')).toBeInTheDocument();
    expect(reloadApp).not.toHaveBeenCalled();
  });

  it('every page shows a band with the way back', async () => {
    await band(VIEWING);
    expect(screen.getByRole('status')).toHaveTextContent('Viewing as a regular user.');
    fireEvent.click(screen.getByRole('button', { name: 'Back to admin' }));
    await waitFor(() => expect(setViewAsUser).toHaveBeenCalledWith(false));
    expect(reloadApp).toHaveBeenCalled();
  });

  it('no band otherwise', async () => {
    await band(ADMIN);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
