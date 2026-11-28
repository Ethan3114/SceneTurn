import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { createStore } from 'zustand/vanilla';

import {
  cloudProjectSessionRepository,
  createProjectSessionStateCreator,
  type ProjectSessionRepository,
  type ProjectSessionState,
} from '@/app/_features/project-session/store';
import { ensureNodeTimeline } from '@/app/_features/node-timeline';
import type { AppNode, SceneTurnProject } from '@/app/_types';
import { createProjectSnapshot, defaultGraphData } from '@/app/_utils/projectPersistence';

function createProject() {
  const graph = defaultGraphData();
  const scene: AppNode = {
    id: crypto.randomUUID(),
    type: 'scene',
    position: { x: 320, y: 180 },
    data: { type: 'scene', title: '后山相遇', bodyText: '萧炎望向山下。' },
  };
  return createProjectSnapshot(null, '项目显示状态验证', [...graph.nodes, scene], [{
    id: crypto.randomUUID(), source: graph.nodes[0].id, target: scene.id, sourceHandle: 'node:default',
  }]);
}

function createSession(project = createProject(), repository = cloudProjectSessionRepository) {
  const store = createStore<ProjectSessionState>()(createProjectSessionStateCreator(repository));
  store.getState().loadProjectSnapshot(project);
  return { store, project, sceneId: project.graphData.nodes[1].id, edgeId: project.graphData.edges[0].id };
}

async function createDiskSession() {
  const project = createProject();
  const directory = path.resolve(import.meta.dirname, '../../../.intermediate/project-session-state');
  await mkdir(directory, { recursive: true });
  const file = path.join(await mkdtemp(path.join(directory, 'project-')), 'project.json');
  await writeFile(file, JSON.stringify(project));
  const readProject = async () => JSON.parse(await readFile(file, 'utf8')) as SceneTurnProject;
  const repository: ProjectSessionRepository = {
    getProject: async (id) => {
      const saved = await readProject();
      if (saved.id !== id) throw new Error('保存目录中的项目标识不匹配');
      return saved;
    },
    saveProject: async (value) => {
      await writeFile(file, JSON.stringify(value));
      return readProject();
    },
  };
  const store = createStore<ProjectSessionState>()(createProjectSessionStateCreator(repository));
  await store.getState().loadProject(project.id);
  return { store, readProject, sceneId: project.graphData.nodes[1].id, edgeId: project.graphData.edges[0].id };
}

describe('项目会话的实际显示状态与保存边界', () => {
  it('节点测量、节点选择与边选择继续更新界面，项目保持已保存', async () => {
    const { store, project, sceneId, edgeId } = createSession();
    store.getState().onNodesChange([
      { type: 'dimensions', id: sceneId, dimensions: { width: 320, height: 240 } },
      { type: 'select', id: sceneId, selected: true },
      { type: 'position', id: sceneId, dragging: false },
    ]);
    store.getState().onEdgesChange([{ type: 'select', id: edgeId, selected: true }]);
    const state = store.getState();
    expect(state.nodes.find((node) => node.id === sceneId)).toMatchObject({
      measured: { width: 320, height: 240 }, selected: true, dragging: false,
    });
    expect(state.edges[0].selected).toBe(true);
    expect(state.dirty).toBe(false);
    expect(state.revision).toBe(0);
    expect(await state.flushPendingChanges()).toBe(project);
  });

  it('相同图内容、相同坐标和拖拽结束通知均不产生新的未保存版本', () => {
    const { store, sceneId } = createSession();
    const graph = store.getState().getGraphSnapshot();
    store.getState().setGraph(graph);
    store.getState().setNodes([...store.getState().nodes]);
    store.getState().onNodesChange([{ type: 'position', id: sceneId, position: { x: 320, y: 180 }, dragging: true }]);
    store.getState().onNodesChange([{ type: 'position', id: sceneId, dragging: false }]);
    expect(store.getState().nodes.find((node) => node.id === sceneId)?.dragging).toBe(false);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().revision).toBe(0);
  });

  it('真实移动与同批显示变化分别保留，重复坐标不会重复增加版本', () => {
    const { store, sceneId } = createSession();
    store.getState().onNodesChange([
      { type: 'position', id: sceneId, position: { x: 510, y: 250 }, dragging: true },
      { type: 'dimensions', id: sceneId, dimensions: { width: 320, height: 240 } },
      { type: 'select', id: sceneId, selected: true },
    ]);
    expect(store.getState().nodes.find((node) => node.id === sceneId)).toMatchObject({ position: { x: 510, y: 250 }, selected: true });
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().revision).toBe(1);
    store.getState().onNodesChange([{ type: 'position', id: sceneId, position: { x: 510, y: 250 }, dragging: false }]);
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().revision).toBe(1);
  });

  it('人工设置节点宽高会标记为未保存，单独更新测量值不会增加版本', () => {
    const { store, sceneId } = createSession();
    store.getState().onNodesChange([{ type: 'dimensions', id: sceneId, dimensions: { width: 480, height: 300 }, setAttributes: true, resizing: true }]);
    expect(store.getState().nodes.find((node) => node.id === sceneId)).toMatchObject({ width: 480, height: 300, resizing: true });
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().revision).toBe(1);
    store.getState().onNodesChange([{ type: 'dimensions', id: sceneId, dimensions: { width: 481, height: 301 }, resizing: false }]);
    expect(store.getState().nodes.find((node) => node.id === sceneId)).toMatchObject({ width: 480, height: 300, measured: { width: 481, height: 301 }, resizing: false });
    expect(store.getState().revision).toBe(1);
  });

  it('场景正文、时间线和剧情连接的实际修改继续增加待保存版本', () => {
    const { store, sceneId, edgeId } = createSession();
    store.getState().updateNodeData(sceneId, { bodyText: '药老从戒指中现身。' });
    store.getState().updateNodeTimeline(sceneId, { ...ensureNodeTimeline(), duration: 8 });
    store.getState().onEdgesChange([{ type: 'remove', id: edgeId }]);
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().revision).toBe(3);
    expect(store.getState().edges).toEqual([]);
    expect(store.getState().nodes.find((node) => node.id === sceneId)?.data).toMatchObject({ bodyText: '药老从戒指中现身。', timeline: { duration: 8 } });
  });

  it('加载远端新增场景后的测量只更新显示状态', () => {
    const { store, project } = createSession();
    const nextScene: AppNode = { id: crypto.randomUUID(), type: 'scene', position: { x: 800, y: 180 }, data: { type: 'scene', title: '拜师' } };
    const incoming = createProjectSnapshot(project, project.title, [...project.graphData.nodes, nextScene], project.graphData.edges);
    incoming.metadata.serverRevision = 9;
    store.getState().loadProjectSnapshot(incoming);
    store.getState().onNodesChange([{ type: 'dimensions', id: nextScene.id, dimensions: { width: 360, height: 250 } }]);
    expect(store.getState().project?.metadata.serverRevision).toBe(9);
    expect(store.getState().nodes).toHaveLength(3);
    expect(store.getState().nodes[2].measured).toEqual({ width: 360, height: 250 });
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().revision).toBe(0);
  });

  it('实际文件写入期间更新的选择与测量在保存完成后仍然保留', async () => {
    const { store, readProject, sceneId, edgeId } = await createDiskSession();
    store.getState().setTitle('已经提交的标题');
    const saving = store.getState().saveNow();
    store.getState().onNodesChange([
      { type: 'select', id: sceneId, selected: true },
      { type: 'dimensions', id: sceneId, dimensions: { width: 370, height: 260 } },
    ]);
    store.getState().onEdgesChange([{ type: 'select', id: edgeId, selected: true }]);
    await saving;
    expect((await readProject()).title).toBe('已经提交的标题');
    expect(store.getState().nodes.find((node) => node.id === sceneId)).toMatchObject({ selected: true, measured: { width: 370, height: 260 } });
    expect(store.getState().edges[0].selected).toBe(true);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().revision).toBe(1);
    expect(store.getState().savedRevision).toBe(1);
  });

  it('实际文件写入期间的正文编辑继续保留为未保存，并能在下一次写入持久保存', async () => {
    const { store, readProject, sceneId } = await createDiskSession();
    store.getState().setTitle('提交标题');
    const saving = store.getState().saveNow();
    store.getState().updateNodeData(sceneId, { bodyText: '保存期间继续输入的完整正文。' });
    store.getState().onNodesChange([{ type: 'select', id: sceneId, selected: true }]);
    await saving;
    expect((await readProject()).graphData.nodes[1].data.bodyText).toBe('萧炎望向山下。');
    expect(store.getState().nodes[1].data.bodyText).toBe('保存期间继续输入的完整正文。');
    expect(store.getState().dirty).toBe(true);
    expect(store.getState().revision).toBe(2);
    expect(store.getState().savedRevision).toBe(1);
    await store.getState().flushPendingChanges();
    expect((await readProject()).graphData.nodes[1].data.bodyText).toBe('保存期间继续输入的完整正文。');
    expect(store.getState().nodes[1].selected).toBe(true);
    expect(store.getState().dirty).toBe(false);
    expect(store.getState().savedRevision).toBe(2);
  });
});
