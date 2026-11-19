// A reload of a page whose path is also an API path gets the app; the
// app's own API calls to that path still reach the API.
import { describe, expect, it } from 'vitest';
import { isSpaNavigation } from './spaNavigation';

const BROWSER = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8';

describe('isSpaNavigation', () => {
  it('sends a browser navigation to a shared path to the app', () => {
    for (const path of ['/agents', '/chat', '/admin', '/echo', '/demos/abc', '/demos/abc/', '/agents?x=1']) {
      expect(isSpaNavigation('GET', path, BROWSER), path).toBe(true);
    }
  });

  it('leaves API calls and other API paths to the API', () => {
    expect(isSpaNavigation('GET', '/agents', '*/*')).toBe(false);                 // fetch
    expect(isSpaNavigation('GET', '/agents', 'application/json')).toBe(false);
    expect(isSpaNavigation('GET', '/demos/abc', 'text/event-stream')).toBe(false);
    expect(isSpaNavigation('POST', '/agents', BROWSER)).toBe(false);
    for (const path of ['/agents/abc', '/agents/abc/export', '/demos', '/auth/login', '/admin/users', '/rest/x']) {
      expect(isSpaNavigation('GET', path, BROWSER), path).toBe(false);
    }
  });
});
