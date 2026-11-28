import { describe, expect, it, vi } from 'vitest';

import { SceneTurnProject } from '@/app/_types';

const {
  createSceneTurnBridge,
  sceneTurnBridgeMethods,
  registerIpcHandler,
  validateIpcArgs,
} = require('../../shared/ipc-contract');

const project: SceneTurnProject = {
  schemaVersion: 1,
  id: 'project-1',
  title: 'Project',
  graphData: {
    nodes: [],
    edges: [],
  },
  assets: [],
  metadata: {},
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
};

describe('SceneTurn IPC contract', () => {
  it('generates bridge methods from the shared contract channels', async () => {
    const invoke = vi.fn(async () => project);
    const bridge = createSceneTurnBridge(invoke);

    expect(sceneTurnBridgeMethods).toContain('saveProject');
    await expect(bridge.saveProject(project)).resolves.toEqual(project);
    expect(invoke).toHaveBeenCalledWith('sceneturn:save-project', project);
  });

  it('rejects invalid filesystem-facing arguments before handlers run', () => {
    expect(() => validateIpcArgs('importAsset', [''])).toThrow(/Invalid SceneTurn IPC args/);
    expect(() => validateIpcArgs('saveProject', [{ ...project, title: '' }])).toThrow(/Invalid SceneTurn IPC args/);
  });

  it('validates registered handler arguments and return values', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    const ipcMain = {
      handle: (channel: string, handler: (...args: unknown[]) => unknown) => handlers.set(channel, handler),
    };

    registerIpcHandler(ipcMain, 'saveProject', async (_event: unknown, nextProject: SceneTurnProject) => nextProject);

    await expect(handlers.get('sceneturn:save-project')?.({}, project)).resolves.toEqual(project);
    await expect(handlers.get('sceneturn:save-project')?.({}, { ...project, assets: 'bad' })).rejects.toThrow(/Invalid SceneTurn IPC args/);

    registerIpcHandler(ipcMain, 'importAsset', async () => ({ id: 'asset-1' }));

    await expect(handlers.get('sceneturn:import-asset')?.({}, 'D:\\clip.mp4')).rejects.toThrow(/Invalid SceneTurn IPC result/);
  });
});
