// The run lists polled GET /runs every 5 s on every open page, live run or
// not, hidden tab or not.
import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import usePollRuns, { IDLE_POLL_MS, LIVE_POLL_MS } from './usePollRuns';

let visibility = 'visible';

function setVisibility(v) {
  visibility = v;
  document.dispatchEvent(new Event('visibilitychange'));
}

const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe('usePollRuns', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, get: () => visibility,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    delete document.visibilityState;
  });

  it('polls every 5 s while a run is live, then every 60 s', async () => {
    let runs = [{ status: 'running' }, { status: 'success' }];
    const fetchRuns = vi.fn(async () => runs);
    renderHook(() => usePollRuns(fetchRuns));
    await advance(0);
    expect(fetchRuns).toHaveBeenCalledTimes(1);

    await advance(LIVE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(2);
    runs = [{ status: 'queued' }];
    await advance(LIVE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(3);     // queued is live too

    runs = [{ status: 'success' }, { status: 'error' }];
    await advance(LIVE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(4);     // finds nothing live
    await advance(LIVE_POLL_MS * 2);
    expect(fetchRuns).toHaveBeenCalledTimes(4);
    await advance(IDLE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(5);
  });

  it('polls slowly with nothing running, or when the list fails', async () => {
    const fetchRuns = vi.fn(async () => null);
    renderHook(() => usePollRuns(fetchRuns));
    await advance(IDLE_POLL_MS - 1);
    expect(fetchRuns).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(fetchRuns).toHaveBeenCalledTimes(2);
  });

  it('does not poll while the tab is hidden, and reads at once on return', async () => {
    const fetchRuns = vi.fn(async () => [{ status: 'running' }]);
    renderHook(() => usePollRuns(fetchRuns));
    await advance(0);
    expect(fetchRuns).toHaveBeenCalledTimes(1);

    setVisibility('hidden');
    await advance(IDLE_POLL_MS * 3);
    expect(fetchRuns).toHaveBeenCalledTimes(1);

    await act(async () => { setVisibility('visible'); });
    expect(fetchRuns).toHaveBeenCalledTimes(2);
    await advance(LIVE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(3);
  });

  it('does not start while hidden', async () => {
    visibility = 'hidden';
    const fetchRuns = vi.fn(async () => []);
    renderHook(() => usePollRuns(fetchRuns));
    await advance(IDLE_POLL_MS * 2);
    expect(fetchRuns).not.toHaveBeenCalled();
    await act(async () => { setVisibility('visible'); });
    expect(fetchRuns).toHaveBeenCalledTimes(1);
  });

  it('stops on unmount', async () => {
    const fetchRuns = vi.fn(async () => [{ status: 'running' }]);
    const { unmount } = renderHook(() => usePollRuns(fetchRuns));
    await advance(0);
    unmount();
    await advance(IDLE_POLL_MS);
    expect(fetchRuns).toHaveBeenCalledTimes(1);
  });
});

describe('usePollRuns restarting', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('tells a fetch from before a restart that it is stale', async () => {
    const seen = [];
    let release;
    const fetchRuns = vi.fn((isStale) => new Promise((resolve) => {
      release = () => { seen.push(isStale()); resolve([]); };
    }));
    const { rerender } = renderHook(({ n }) => usePollRuns(fetchRuns, [n]), { initialProps: { n: 0 } });
    const first = release;
    rerender({ n: 1 });
    await act(async () => { first(); release(); });
    expect(seen).toEqual([true, false]);
  });
});
