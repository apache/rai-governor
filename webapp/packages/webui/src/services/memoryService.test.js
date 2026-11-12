// Memory client (ISSUE-026).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import memoryService from './memoryService';

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });

describe('memoryService', () => {
  const original = globalThis.fetch;
  beforeEach(() => {
    globalThis.fetch = vi.fn();
    memoryService._resetEnabled();
  });
  afterEach(() => { globalThis.fetch = original; });

  it('builds a search query from only the filters given', async () => {
    globalThis.fetch.mockResolvedValue(ok({ hits: [] }));
    await memoryService.search('dom-scan', { q: 'sql report', trustTier: 'raw', includeInactive: true, order: 'oldest' });
    const url = new URL(globalThis.fetch.mock.calls[0][0], 'http://x');
    expect(url.pathname).toMatch(/\/memory\/dom-scan\/search$/);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      q: 'sql report', trust_tier: 'raw', include_inactive: 'true', order: 'oldest', limit: '50',
    });
  });

  it('sends a review with the API field names', async () => {
    globalThis.fetch.mockResolvedValue(ok({}));
    await memoryService.review('mem-scan-abc12345', { action: 'verify', why: 'checked', reviewBy: '2027-02-01' });
    const [url, init] = globalThis.fetch.mock.calls[0];
    expect(url).toMatch(/\/memory\/entries\/mem-scan-abc12345\/review$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ action: 'verify', why: 'checked', review_by: '2027-02-01' });
  });

  it('surfaces the reason and status of a refusal', async () => {
    globalThis.fetch.mockResolvedValue(fail(403, 'You wrote this entry; someone else has to review it'));
    const err = await memoryService.review('mem-scan-abc12345', { action: 'keep' }).catch((e) => e);
    expect(err.message).toMatch(/someone else/);
    expect(err.status).toBe(403);
  });

  it('asks once whether memory is on, and says no when it is not mounted', async () => {
    globalThis.fetch.mockResolvedValue(fail(404, 'Not Found'));
    expect(await memoryService.isEnabled()).toBe(false);
    expect(await memoryService.isEnabled()).toBe(false);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it('asks the status route, which counts nothing, not the domain list', async () => {
    globalThis.fetch.mockResolvedValue(ok({ enabled: true }));
    expect(await memoryService.isEnabled()).toBe(true);
    expect(new URL(globalThis.fetch.mock.calls[0][0], 'http://x').pathname).toMatch(/\/memory\/status$/);
  });
});
