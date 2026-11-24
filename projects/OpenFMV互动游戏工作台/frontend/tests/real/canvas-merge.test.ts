import { expect, inject, it } from 'vitest';

import type { CanvasSnapshot, ProjectEnvelope } from '@/app/_utils/cloudApi';
import { canMergeCanvasEdits, mergeCanvasSnapshots } from '@/app/_features/creation-canvas/merge-canvas';

const origin = inject('isolatedApiOrigin');

async function actualCanvas() {
  const registered = await fetch(`${origin}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: `canvas-merge-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID() }) });
  expect(registered.status).toBe(201);
  const cookie = registered.headers.getSetCookie().map((item) => item.split(';')[0]).join('; ');
  const headers = { 'Content-Type': 'application/json', Cookie: cookie };
  const created = await fetch(`${origin}/projects`, { method: 'POST', headers, body: JSON.stringify({ title: '画布并行保存验证' }) });
  expect(created.status).toBe(201);
  const project = await created.json() as ProjectEnvelope;
  const sceneId = project.project.graphData.nodes.find((node) => node.type === 'scene')?.id;
  if (!sceneId) throw new Error('真实项目没有剧情场景');
  const textId = crypto.randomUUID(), outputId = crypto.randomUUID();
  const canvas: CanvasSnapshot = { sceneId, nodes: [
    { id: textId, type: 'text', title: '输入文字', position: { x: 40, y: 40 }, width: 300, height: 220, metadata: { content: '已保存的原文' } },
    { id: outputId, type: 'text', title: '另一页面的文字', position: { x: 450, y: 40 }, width: 300, height: 220, metadata: { content: '' } },
  ], connections: [], viewport: { x: 20, y: 80, k: 1 } };
  const save = async (value: CanvasSnapshot, revision: number) => {
    const response = await fetch(`${origin}/projects/${project.project.id}/canvases/${sceneId}`, { method: 'PUT', headers, body: JSON.stringify({ nodes: value.nodes, connections: value.connections, viewport: value.viewport, expectedRevision: revision, operationId: crypto.randomUUID() }) });
    expect(response.status).toBe(200); return await response.json() as { canvas: CanvasSnapshot; revision: number };
  };
  const baseline = await save(canvas, project.revision);
  const read = async () => {
    const response = await fetch(`${origin}/projects/${project.project.id}/canvases/${sceneId}`, { headers }); expect(response.status).toBe(200); return await response.json() as { canvas: CanvasSnapshot; revision: number };
  };
  const logout = async () => { expect((await fetch(`${origin}/auth/logout`, { method: 'POST', headers })).status).toBe(200); };
  return { baseline, save, read, logout, textId, outputId };
}

it('真实服务的新文字版本与本地视口调整同时保留，后续保存使用新版本', async () => {
  const actual = await actualCanvas();
  const local = structuredClone(actual.baseline.canvas);
  local.viewport = { x: 111, y: 127, k: .73 };
  const remote = structuredClone(actual.baseline.canvas);
  remote.nodes[1].metadata = { content: '另一页面通过正式接口保存的完整文字', status: 'success' };
  await actual.save(remote, actual.baseline.revision);
  const incoming = await actual.read();
  expect(canMergeCanvasEdits(actual.baseline.canvas, local, incoming.canvas)).toBe(true);
  const merged = mergeCanvasSnapshots(actual.baseline.canvas, local, incoming.canvas);
  expect(merged.viewport).toEqual(local.viewport);
  expect(merged.nodes[1].metadata?.content).toBe(remote.nodes[1].metadata?.content);
  const saved = await actual.save(merged, incoming.revision);
  expect((await actual.read()).canvas).toEqual(saved.canvas);
  await actual.logout();
});

it('真实HTTP返回期间新增的正文与节点移动保留，同时接收其他字段', async () => {
  const actual = await actualCanvas();
  const local = structuredClone(actual.baseline.canvas), remote = structuredClone(actual.baseline.canvas);
  remote.nodes[1].metadata = { content: '已在服务端保存的文字', status: 'success' };
  const saving = actual.save(remote, actual.baseline.revision);
  local.nodes[0].metadata = { content: '请求期间继续输入的文字' };
  local.nodes[1].position = { x: 512, y: 160 };
  await saving;
  const incoming = await actual.read();
  expect(canMergeCanvasEdits(actual.baseline.canvas, local, incoming.canvas)).toBe(true);
  const merged = mergeCanvasSnapshots(actual.baseline.canvas, local, incoming.canvas);
  expect(merged.nodes[0].metadata?.content).toBe(local.nodes[0].metadata?.content);
  expect(merged.nodes[1].position).toEqual(local.nodes[1].position);
  expect(merged.nodes[1].metadata?.content).toBe(remote.nodes[1].metadata?.content);
  await actual.save(merged, incoming.revision);
  expect((await actual.read()).canvas).toEqual(merged);
  await actual.logout();
});

it('同一正文的并行修改保留本地输入，服务端新增节点仍然保留', async () => {
  const actual = await actualCanvas();
  const local = structuredClone(actual.baseline.canvas), remote = structuredClone(actual.baseline.canvas);
  local.nodes[0].metadata = { content: '当前页面继续编辑的正文' };
  remote.nodes[0].metadata = { content: '其他页面已经保存的正文', fontSize: 18 };
  remote.nodes.push({ id: crypto.randomUUID(), type: 'text', title: '新文字节点', position: { x: 800, y: 40 }, width: 300, height: 220, metadata: { content: '正式接口新增的内容' } });
  await actual.save(remote, actual.baseline.revision);
  const incoming = await actual.read();
  expect(canMergeCanvasEdits(actual.baseline.canvas, local, incoming.canvas)).toBe(false);
  const merged = mergeCanvasSnapshots(actual.baseline.canvas, local, incoming.canvas);
  expect(merged.nodes).toHaveLength(3);
  expect(merged.nodes[0].metadata?.content).toBe(local.nodes[0].metadata?.content);
  expect(merged.nodes[0].metadata?.fontSize).toBe(18);
  await actual.save(merged, incoming.revision);
  expect((await actual.read()).canvas).toEqual(merged);
  await actual.logout();
});

it('本地删除节点后过滤服务端指向该节点的新连接，正式保存通过', async () => {
  const actual = await actualCanvas();
  const local = structuredClone(actual.baseline.canvas), remote = structuredClone(actual.baseline.canvas);
  local.nodes = local.nodes.filter((node) => node.id !== actual.outputId);
  remote.connections.push({ id: crypto.randomUUID(), fromNodeId: actual.textId, toNodeId: actual.outputId });
  await actual.save(remote, actual.baseline.revision);
  const incoming = await actual.read();
  expect(incoming.canvas.connections).toHaveLength(1);
  expect(canMergeCanvasEdits(actual.baseline.canvas, local, incoming.canvas)).toBe(false);
  const merged = mergeCanvasSnapshots(actual.baseline.canvas, local, incoming.canvas);
  expect(merged.nodes.map((node) => node.id)).toEqual([actual.textId]);
  expect(merged.connections).toHaveLength(0);
  await actual.save(merged, incoming.revision);
  expect((await actual.read()).canvas).toEqual(merged);
  await actual.logout();
});
