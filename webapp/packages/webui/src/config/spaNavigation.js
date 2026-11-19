// Paths that are both a page of this app and an API path: the API is
// mounted at the root (no /api prefix), so /agents, /chat, /admin, /echo
// and /demos/<id> name a page and an API route at once. A browser
// navigation to one (a reload, a link opened in a new tab) asks for HTML
// and must get the app; fetch and XHR don't ask for HTML and must reach
// the API. Mirrored in webapp/infra/docker/nginx.conf ($spa_navigation):
// change both together.
export const SPA_API_PATHS = /^\/(agents|chat|admin|echo|demos\/[^/]+)\/?$/;

export function isSpaNavigation(method, url, accept) {
  if (method !== 'GET' || !(accept || '').includes('text/html')) return false;
  const path = (url || '').split('?')[0];
  return SPA_API_PATHS.test(path);
}
