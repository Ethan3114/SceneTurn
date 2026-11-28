import { describe, expect, it } from 'vitest';

import { buildTimelineAssetLibraryItems } from '@/app/_features/node-timeline/assetLibrary';
import { SceneTurnAsset, SceneTurnProject } from '@/app/_types';

const asset = (patch: Partial<SceneTurnAsset>): SceneTurnAsset => ({
  id: 'asset-1',
  type: 'video',
  name: '01 opening.mp4',
  path: 'file:///D:/SceneTurn/assets/a.mp4',
  relativePath: 'file:///D:/SceneTurn/assets/a.mp4',
  importedAt: '2026-01-01T00:00:00.000Z',
  metadata: { originalPath: 'D:\\media\\01 opening.mp4', duration: 7.06 },
  ...patch,
});

const project = (patch: Partial<SceneTurnProject>): SceneTurnProject => ({
  schemaVersion: 1,
  id: 'project-1',
  title: 'Project',
  graphData: { nodes: [], edges: [] },
  assets: [],
  metadata: {},
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:00:00.000Z',
  ...patch,
});

describe('node timeline asset library', () => {
  it('deduplicates matching assets across projects and prefers the active project', () => {
    const items = buildTimelineAssetLibraryItems({
      currentProjectId: 'active',
      projects: [
        project({
          id: 'other',
          title: 'Untitled project',
          assets: [asset({ id: 'foreign-copy', path: 'file:///D:/SceneTurn/assets/foreign.mp4' })],
        }),
        project({
          id: 'active',
          title: '未命名项目',
          assets: [asset({ id: 'active-copy', path: 'file:///D:/SceneTurn/assets/active.mp4' })],
        }),
      ],
    });

    expect(items).toHaveLength(1);
    expect(items[0].projectId).toBe('active');
    expect(items[0].projectTitle).toBe('未命名项目');
  });
});
