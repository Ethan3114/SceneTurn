import { describe, expect, inject, it } from 'vitest';
import { compileRuntimeGraph, createRuntimeState, getRuntimeSnapshot } from '@/app/_utils/graphRuntime';
import type { SceneTurnProject } from '@/app/_types';
import { createStore } from 'zustand/vanilla';
import { createProjectSessionStateCreator, type ProjectSessionRepository, type ProjectSessionState } from '@/app/_features/project-session/store';
import { collectProjectAssetsFromGraph } from '@/app/_utils/projectPersistence';
import { createMediaClipFromTimelineAsset, ensureNodeTimeline, insertTimelineClip } from '@/app/_features/node-timeline';
import { mkdtemp, readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { getAssetSource } from '@/app/_utils/assetIdentity';

const origin = inject('isolatedApiOrigin');

describe('真实项目与前端运行时契约', () => {
  it('读取真实服务创建的项目，编译其NodeTimeline并保存后恢复', async () => {
    const registration = await fetch(`${origin}/auth/register`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: `runtime-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID() }),
    });
    expect(registration.ok).toBe(true);
    const cookie = registration.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ');
    expect(cookie).toContain('sceneturn_session=');
    const created = await fetch(`${origin}/projects`, { method: 'POST', headers: { 'Content-Type': 'application/json', Cookie: cookie }, body: JSON.stringify({ title: '真实运行时契约验证' }) });
    expect(created.ok).toBe(true);
    const envelope = await created.json() as { project: SceneTurnProject; revision: number };
    const program = compileRuntimeGraph(envelope.project.graphData);
    const state = createRuntimeState(program);
    const snapshot = getRuntimeSnapshot(program, state);
    expect(snapshot.currentNode?.type).toBe('start');
    const saved = await fetch(`${origin}/projects/${envelope.project.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Cookie: cookie }, body: JSON.stringify({ expectedRevision: envelope.revision, operationId: crypto.randomUUID(), project: envelope.project }) });
    expect(saved.ok).toBe(true);
    const restored = await fetch(`${origin}/projects/${envelope.project.id}`, { headers: { Cookie: cookie } });
    const result = await restored.json() as { project: SceneTurnProject; revision: number };
    expect(result.project.graphData).toEqual(envelope.project.graphData);
    expect(result.revision).toBeGreaterThan(envelope.revision);
    const exited = await fetch(`${origin}/auth/logout`, { method: 'POST', headers: { Cookie: cookie } });
    expect(exited.ok).toBe(true);
    const revoked = await fetch(`${origin}/projects/${envelope.project.id}`, { headers: { Cookie: cookie } });
    expect(revoked.status).toBe(401);
  });

  it('实际HTTP保存期间的新编辑保持未保存状态，后续保存取得正确版本', async () => {
    const registered = await fetch(`${origin}/auth/register`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: `saving-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID() }),
    });
    expect(registered.ok).toBe(true);
    const cookie = registered.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ');
    const headers = { 'Content-Type': 'application/json', Cookie: cookie };
    const created = await fetch(`${origin}/projects`, { method: 'POST', headers, body: JSON.stringify({ title: '保存期间编辑验证' }) });
    expect(created.ok).toBe(true);
    const initial = await created.json() as { project: SceneTurnProject; revision: number };
    const repository: ProjectSessionRepository = {
      getProject: async (id) => {
        const response = await fetch(`${origin}/projects/${id}`, { headers });
        expect(response.ok).toBe(true);
        return (await response.json() as { project: SceneTurnProject }).project;
      },
      saveProject: async (project) => {
        const response = await fetch(`${origin}/projects/${project.id}`, {
          method: 'PUT', headers,
          body: JSON.stringify({ expectedRevision: project.metadata.serverRevision, operationId: crypto.randomUUID(), project }),
        });
        expect(response.ok).toBe(true);
        return (await response.json() as { project: SceneTurnProject }).project;
      },
    };
    const store = createStore<ProjectSessionState>()(createProjectSessionStateCreator(repository));
    await store.getState().loadProject(initial.project.id);
    store.getState().setTitle('提交时的标题');
    const saving = store.getState().saveNow();
    store.getState().setTitle('保存期间的新标题');
    await saving;
    expect(store.getState().title).toBe('保存期间的新标题');
    expect(store.getState().dirty).toBe(true);
    expect((await repository.getProject(initial.project.id))?.title).toBe('提交时的标题');
    await store.getState().saveNow();
    expect(store.getState().dirty).toBe(false);
    expect((await repository.getProject(initial.project.id))?.title).toBe('保存期间的新标题');
    await fetch(`${origin}/auth/logout`, { method: 'POST', headers });
  });

  it('真实视频上传后的素材路径与时间线引用保持同一标识', async () => {
    const directory = inject('isolatedApiScratch');
    const mediaDirectory = await mkdtemp(path.join(directory, 'frontend-media-'));
    const mediaPath = path.join(mediaDirectory, 'timeline.mp4');
    await promisify(execFile)('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', mediaPath]);
    const registered = await fetch(`${origin}/auth/register`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: `media-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID() }),
    });
    expect(registered.ok).toBe(true);
    const cookie = registered.headers.getSetCookie().map((value) => value.split(';')[0]).join('; ');
    const headers = { 'Content-Type': 'application/json', Cookie: cookie };
    const created = await fetch(`${origin}/projects`, { method: 'POST', headers, body: JSON.stringify({ title: '真实媒体时间线验证' }) });
    expect(created.ok).toBe(true);
    const initial = await created.json() as { project: SceneTurnProject };
    const upload = new FormData();
    upload.append('file', new Blob([await readFile(mediaPath)], { type: 'video/mp4' }), 'timeline.mp4');
    const imported = await fetch(`${origin}/projects/${initial.project.id}/assets`, { method: 'POST', headers: { Cookie: cookie }, body: upload });
    expect(imported.ok).toBe(true);
    const result = await imported.json() as { project: SceneTurnProject; revision: number; asset: { id: string; durationMs: number } };
    expect(result.asset.durationMs).toBeGreaterThan(0);
    const asset = result.project.assets.find((item) => item.id === result.asset.id);
    expect(asset).toBeDefined();
    const scene = result.project.graphData.nodes.find((node) => node.type === 'scene');
    if (!asset || !scene) throw new Error('服务端项目缺少场景或已上传素材。');
    expect(getAssetSource(asset)).toBe(asset.path);
    const clip = createMediaClipFromTimelineAsset({ type: 'video', src: getAssetSource(asset), assetId: asset.id, name: asset.name, startTime: 0, metadata: asset.metadata });
    scene.data.timeline = insertTimelineClip({ timeline: ensureNodeTimeline(scene.data.timeline), clip });
    result.project.assets = collectProjectAssetsFromGraph(result.project.graphData, result.project.assets);
    expect(result.project.assets).toHaveLength(1);
    expect(result.project.assets[0].id).toBe(result.asset.id);
    const saved = await fetch(`${origin}/projects/${result.project.id}`, { method: 'PUT', headers, body: JSON.stringify({ expectedRevision: result.revision, operationId: crypto.randomUUID(), project: result.project }) });
    expect(saved.ok).toBe(true);
    const restored = await fetch(`${origin}/projects/${result.project.id}`, { headers });
    const restoredProject = (await restored.json() as { project: SceneTurnProject }).project;
    expect(restoredProject.assets.map((item) => item.id)).toEqual([asset.id]);
    expect(restoredProject.graphData.nodes.find((node) => node.id === scene.id)?.data.timeline?.tracks.flatMap((track) => track.clips).find((item) => item.id === clip.id)).toMatchObject({ assetId: asset.id, src: asset.path });
    await fetch(`${origin}/auth/logout`, { method: 'POST', headers });
  });
});
