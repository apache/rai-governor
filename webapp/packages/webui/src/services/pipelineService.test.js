// Pipeline import/export client (ISSUE-011).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import pipelineService, { sourceText } from './pipelineService';

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const fail = (status, detail) => ({ ok: false, status, json: async () => ({ detail }) });

describe('pipelineService', () => {
  const original = globalThis.fetch;
  beforeEach(() => { globalThis.fetch = vi.fn(); });
  afterEach(() => { globalThis.fetch = original; });

  it('sends a GitHub import as JSON, leaving out an empty token and label', async () => {
    globalThis.fetch.mockResolvedValue(ok({ agents: [] }));
    await pipelineService.importFromGithub({ repo: 'apache/tooling-agents', ref: '', path: 'ASVS', dryRun: true });
    const [url, init] = globalThis.fetch.mock.calls[0];
    expect(url).toMatch(/\/pipelines\/import\/github$/);
    expect(JSON.parse(init.body)).toEqual({ repo: 'apache/tooling-agents', ref: 'main', path: 'ASVS', dryRun: true, deploy: true, labeled: true });
  });

  it('uploads each file under its path inside the picked folder', async () => {
    globalThis.fetch.mockResolvedValue(ok({ agents: [] }));
    const a = new File(['pipeline: x'], 'pipeline.yaml');
    Object.defineProperty(a, 'webkitRelativePath', { value: 'ASVS/pipeline.yaml' });
    const b = new File(['code'], 'a.py');
    Object.defineProperty(b, 'webkitRelativePath', { value: 'ASVS/agents/a.py' });
    await pipelineService.importFromFolder({ files: [a, b], label: 'v1', dryRun: false });
    const form = globalThis.fetch.mock.calls[0][1].body;
    expect(form.getAll('files').map((f) => f.name)).toEqual(['ASVS/pipeline.yaml', 'ASVS/agents/a.py']);
    expect(form.get('label')).toBe('v1');
    expect(form.get('dryRun')).toBe('false');
    expect(form.get('deploy')).toBe('true');
    expect(form.get('labeled')).toBe('true');
  });

  it('sends labeled false for the stable copy', async () => {
    globalThis.fetch.mockResolvedValue(ok({ agents: [] }));
    await pipelineService.importFromGithub({ repo: 'a/b', dryRun: true, labeled: false });
    expect(JSON.parse(globalThis.fetch.mock.calls[0][1].body).labeled).toBe(false);
    await pipelineService.importFromFolder({ files: [], dryRun: true, labeled: false });
    expect(globalThis.fetch.mock.calls[1][1].body.get('labeled')).toBe('false');
  });

  it('sends deploy false when asked not to deploy', async () => {
    globalThis.fetch.mockResolvedValue(ok({ agents: [] }));
    await pipelineService.importFromGithub({ repo: 'a/b', dryRun: false, deploy: false });
    expect(JSON.parse(globalThis.fetch.mock.calls[0][1].body).deploy).toBe(false);
  });

  it('surfaces the API reason on failure', async () => {
    globalThis.fetch.mockResolvedValue(fail(400, "no pipeline.yaml found"));
    await expect(pipelineService.importFromGithub({ repo: 'a/b', dryRun: true }))
      .rejects.toThrow('no pipeline.yaml found');
  });

  it('describes where a copy came from', () => {
    expect(sourceText({ kind: 'github', repo: 'apache/tooling-agents', path: 'ASVS', commit: '3194087abc' }))
      .toBe('apache/tooling-agents/ASVS @ 3194087');
    expect(sourceText({ kind: 'upload', files: 3 })).toBe('folder upload');
    expect(sourceText(null)).toBe('');
  });
});
