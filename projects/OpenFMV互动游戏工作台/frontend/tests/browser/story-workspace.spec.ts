import { expect, test, type Page } from '@playwright/test';
import { readFile, readdir, mkdir } from 'node:fs/promises';
import path from 'node:path';
import type { ProjectEnvelope } from '../../app/_utils/cloudApi';
import { restoreLegacyCanvasAssistant } from './manual-studio';

const proofDirectory = path.resolve('../.intermediate/agent-smoke/279439cb-7838-4bda-960d-026e6c38b764');
test.use({ viewport: { width: 1440, height: 1000 } });

async function createActualProject(page: Page) {
  const proof = JSON.parse(await readFile(path.join(proofDirectory, 'proof.json'), 'utf8'));
  expect(proof.completed && proof.restartVerified).toBe(true);
  const source = JSON.parse(await readFile(path.join(proofDirectory, 'project.json'), 'utf8')) as ProjectEnvelope;
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`story-browser-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  const created = await page.request.post('/api/v1/projects', { data: { title: '真实Flash故事工作流·浏览器验证' } });
  expect(created.status()).toBe(201);
  let envelope = await created.json() as ProjectEnvelope;
  const mediaDirectory = path.join(proofDirectory, 'data/media');
  const filename = (await readdir(mediaDirectory)).find((name) => name.endsWith('.mp4'));
  if (!filename) throw new Error('真实验证缺少原视频');
  const upload = await page.request.post(`/api/v1/projects/${envelope.project.id}/assets`, { multipart: { file: { name: '真实树林.mp4', mimeType: 'video/mp4', buffer: await readFile(path.join(mediaDirectory, filename)) } } });
  expect(upload.status()).toBe(201);
  envelope = await upload.json() as ProjectEnvelope;
  const asset = envelope.project.assets[0];
  const graph = structuredClone(source.project.graphData);
  for (const scene of graph.nodes) for (const track of scene.data.timeline?.tracks || []) for (const clip of track.clips) {
    if (clip.type === 'video') { clip.assetId = asset.id; clip.src = asset.path; }
  }
  for (const edge of graph.edges) edge.sourceHandle ||= 'node:default';
  const project = { ...envelope.project, graphData: graph, metadata: { ...envelope.project.metadata, entryNodeId: graph.nodes.find((node) => node.type === 'start')?.id } };
  const saved = await page.request.put(`/api/v1/projects/${project.id}`, { data: { project, expectedRevision: envelope.revision, operationId: crypto.randomUUID() } });
  expect(saved.status()).toBe(200);
  envelope = await saved.json() as ProjectEnvelope;
  return { envelope, asset };
}

test('问问AI持续保存本人Codex对话、停止记录与当前场景', async ({ page }) => {
  test.setTimeout(90000);
  const { envelope } = await createActualProject(page);
  const id = envelope.project.id;
  await page.goto(`/zh-CN/editor?id=${id}`);
  await page.getByRole('button', { name: '问问 AI', exact: true }).click();
  const chat = page.getByTestId('project-codex-chat');
  await expect(chat.getByTestId('chat-input')).toBeEnabled();
  await chat.getByTestId('chat-input').fill('读取当前场景的真实时间线，本次等待本人连接。');
  await chat.getByTestId('chat-send').click();
  await expect(chat.getByTestId('chat-run-status')).toHaveText('等待本人Codex连接');
  await chat.getByTestId('chat-stop').click();
  await expect(chat.getByTestId('chat-run-status')).toHaveText('已停止');
  await expect(chat.getByTestId('chat-message-user')).toContainText('读取当前场景');
  const conversations = await (await page.request.get(`/api/v1/projects/${id}/conversations`)).json();
  const saved = await (await page.request.get(`/api/v1/projects/${id}/conversations/${conversations.conversations[0].id}`)).json();
  expect(saved.conversation.run.executor).toBe('codex');
  expect(saved.conversation.run.toolCatalogVersion).toBe('R4');
  expect(saved.conversation.run.calls).toEqual([]);
  expect(saved.conversation.messages[0].context.surface).toBe('editor');
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const directory = path.resolve('../docs/evidence/阶段2-2'); await mkdir(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, `总画布Agent-${width}px.png`) });
  }
  await page.reload(); await page.getByRole('button', { name: '问问 AI', exact: true }).click();
  await expect(page.getByTestId('chat-run-status')).toHaveText('已停止');
  const scene = envelope.project.graphData.nodes.find((node) => node.type === 'scene')!;
  await page.goto(`/zh-CN/studio?id=${id}&sceneId=${scene.id}`);
  await expect(page.getByTestId('canvas-add-node')).toBeEnabled();
  await expect(page.locator('[data-agent-node]')).toHaveCount(0);
  await restoreLegacyCanvasAssistant(page);
  await expect(page.locator('[data-agent-node]').getByTestId('agent-role').locator('option')).toHaveCount(6);
});

test('真实Flash保存的按钮和QTE在浏览器跳转', async ({ page }) => {
  test.setTimeout(90000);
  const { envelope } = await createActualProject(page);
  const id = envelope.project.id;
  const first = envelope.project.graphData.nodes.find((node) => node.type === 'scene' && node.data.timeline?.tracks.some((track) => track.clips.some((clip) => clip.type === 'video')))!;
  const buttons = first.data.timeline!.tracks.flatMap((track) => track.clips).filter((clip) => clip.type === 'button');
  const normal = buttons.find((clip) => clip.mode !== 'qte')!;
  const qte = buttons.find((clip) => clip.mode === 'qte')!;
  async function start() {
    await page.goto(`/zh-CN/editor?id=${id}`);
    await page.getByRole('button', { name: '试玩', exact: true }).click();
    await page.getByRole('button', { name: '继续', exact: true }).click();
  }
  async function checkTarget(clipId: string, outcome: 'click' | 'timeout') {
    const edge = envelope.project.graphData.edges.find((edge) => edge.source === first.id && edge.sourceHandle === `button:${clipId}:${outcome}`)!;
    const target = envelope.project.graphData.nodes.find((node) => node.id === edge.target)!;
    if (target.type === 'end') await expect(page.getByRole('button', { name: '重新开始', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Info', exact: true }).click();
    const title = target.data.type === 'scene' ? target.data.title : target.data.label;
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  }
  await start();
  await expect(page.locator('video')).toHaveCount(1);
  await page.getByRole('button', { name: normal.type === 'button' ? normal.label : '' , exact: true }).click();
  await checkTarget(normal.id, 'click');
  normal.enabled = false;
  const saved = await page.request.put(`/api/v1/projects/${id}`, { data: { project: envelope.project, expectedRevision: envelope.revision, operationId: crypto.randomUUID() } });
  expect(saved.status()).toBe(200);
  await start();
  await page.locator('[data-qte-input="click"]').click();
  await checkTarget(qte.id, 'click');
  await start();
  await expect(page.locator('[data-qte-input="click"]')).toBeVisible();
  await expect(page.locator('[data-qte-input="click"]')).toHaveCount(0, { timeout: 10000 });
  await checkTarget(qte.id, 'timeout');
});

test('小画布实际媒体加入场景时间线，保留已有互动', async ({ page }) => {
  const { envelope, asset } = await createActualProject(page);
  const id = envelope.project.id;
  const scene = envelope.project.graphData.nodes.find((node) => node.type === 'scene' && node.data.timeline?.duration)!;
  const nodeId = crypto.randomUUID();
  const saved = await page.request.put(`/api/v1/projects/${id}/canvases/${scene.id}`, { data: { expectedRevision: envelope.revision, operationId: crypto.randomUUID(),
    nodes: [{ id: nodeId, type: 'video', title: '真实树林素材', position: { x: 80, y: 100 }, width: 360, height: 240, metadata: { storageKey: asset.id, content: asset.path, status: 'success' } }], connections: [], viewport: { x: 20, y: 20, k: 0.8 } } });
  expect(saved.status()).toBe(200);
  await page.goto(`/zh-CN/studio?id=${id}&sceneId=${scene.id}`);
  await page.locator(`[data-node-id="${nodeId}"]`).getByTestId('canvas-node-header').click();
  await page.getByTestId('canvas-adopt-to-timeline').click();
  await expect(page.getByText('素材已加入当前场景时间线，可在故事总画布中剪辑和设置互动。')).toBeVisible();
  const restored = await (await page.request.get(`/api/v1/projects/${id}`)).json() as ProjectEnvelope;
  const clips = restored.project.graphData.nodes.find((node) => node.id === scene.id)!.data.timeline!.tracks.flatMap((track) => track.clips);
  expect(clips.filter((clip) => clip.type === 'video')).toHaveLength(2);
  expect(clips.filter((clip) => clip.type === 'button')).toEqual(scene.data.timeline!.tracks.flatMap((track) => track.clips).filter((clip) => clip.type === 'button'));
});
