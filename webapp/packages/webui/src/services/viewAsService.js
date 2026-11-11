// Viewing the site as a regular user (site admins). The switch lives on the
// login session, so the whole app reloads afterwards: every page, cache and
// the signed-in user then reflect it.
import appConfig from '../config';

export async function setViewAsUser(on) {
  const resp = await fetch(`${appConfig.api.baseUrl}/auth/view-as`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ on }),
  });
  if (!resp.ok) {
    let detail = '';
    try { detail = (await resp.json()).detail; } catch { /* not JSON */ }
    throw new Error(detail || `Couldn't switch (${resp.status})`);
  }
  return resp.json();
}

export function reloadApp() {
  window.location.assign('/');
}
