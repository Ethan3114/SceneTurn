import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { enterManualStudio } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

async function createProject(page: Page, label: string) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`creation-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.getByTestId('project-title').fill(label);
  await page.getByTestId('project-create').click();
  await enterManualStudio(page);
  await expect(page.getByTestId('canvas-add-config')).toBeEnabled();
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('真实项目地址没有项目标识');
  return id;
}

async function cc0Media(configFile: string | undefined) {
  if (!configFile) throw new Error('真实浏览器配置文件路径不可用');
  const product = path.resolve(path.dirname(configFile), '..');
  const directory = path.join(product, '.intermediate/test-media');
  const video = path.join(directory, 'flower.mp4'), image = path.join(directory, 'flower-frame.png');
  await mkdir(directory, { recursive: true });
  if (!existsSync(video)) {
    const response = await fetch('https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4');
    expect(response.ok).toBe(true); await writeFile(video, Buffer.from(await response.arrayBuffer()));
  }
  if (!existsSync(image)) execFileSync('ffmpeg', ['-v', 'error', '-i', video, '-frames:v', '1', '-update', '1', image], { env: { ...process.env, TMPDIR: path.join(product, '.intermediate/tmp') } });
  return { video, image };
}

async function connectNodes(page: Page, from: string, to: string) {
  const source = page.locator(`[data-testid="canvas-node"][data-node-id="${from}"]`);
  await source.getByTestId('canvas-node-body').hover();
  const handle = page.locator(`[data-testid="canvas-connection-source"][data-node-id="${from}"]`);
  await handle.hover();
  const start = await handle.boundingBox(), end = await page.locator(`[data-testid="canvas-connection-target"][data-node-id="${to}"]`).boundingBox();
  if (!start || !end) throw new Error('实际连接端点没有显示');
  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2); await page.mouse.down();
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 12 }); await page.mouse.up();
}

test('生成分类在Config内部编辑，文字计费与图片配置保持真实状态', async ({ page }) => {
  let paidRequests = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && (/\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname) || new URL(request.url()).pathname.endsWith('/text-generations'))) paidRequests++; });
  const projectId = await createProject(page, '节点内功能分类验证');
  await page.getByTestId('canvas-add-config').click();
  const config = page.getByTestId('canvas-node').last();
  const id = await config.getAttribute('data-node-id');
  await config.getByTestId('generation-kind-text').click();
  for (const task of ['text', 'summary', 'character', 'prop', 'scene', 'plot', 'shot']) await expect(config.getByTestId(`generation-task-${task}`)).toBeVisible();
  await expect(config.getByTestId('canvas-node-body').getByTestId('generation-prompt')).toContainText('根据创意编写完整剧本');
  await config.getByTestId('generation-prompt').fill('写一个发生在清晨树林中的互动故事。');
  await expect(config.getByTestId('text-generation-start')).toBeDisabled();
  await expect(config.getByTestId('text-service-status')).toContainText(/尚未配置|尚未开放/);
  await config.getByTestId('generation-kind-image').click();
  for (const task of ['characterImage', 'propImage', 'sceneImage', 'shotImage']) await expect(config.getByTestId(`generation-task-${task}`)).toBeVisible();
  await expect(config.getByTestId('image-service-status')).toContainText('尚未配置');
  await expect(config.getByTestId('generation-quote')).toBeDisabled();
  await config.getByTestId('generation-kind-video').click();
  await config.getByTestId('generation-task-join').click();
  await expect(config.getByTestId('generation-join')).toBeDisabled();
  await expect(config.getByTestId('generation-input-problem')).toContainText('至少两个');
  await config.getByTestId('generation-task-clip').click();
  await expect(config.getByTestId('generation-prompt')).toContainText('清晨树林');
  await expect(config.locator('[data-node-panel] [contenteditable="true"]')).toHaveCount(0);
  await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.reload();
  const restored = page.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`);
  await expect(restored.getByTestId('canvas-node-body').getByTestId('generation-prompt')).toContainText('清晨树林');
  const stored = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
  expect(stored.canvases.flatMap((canvas: { nodes: Array<{ id: string; metadata: Record<string, unknown> }> }) => canvas.nodes).find((node: { id: string }) => node.id === id).metadata.dramaTask).toBe('clip');
  await page.goto('/zh-CN/settings/text');
  await expect(page.getByTestId('text-settings-status')).toBeVisible(); await expect(page.getByTestId('text-api-key')).toHaveCount(0);
  await expect(page.getByTestId('text-price-table')).toHaveCount(0);
  expect(paidRequests).toBe(0);
});

test('图片切片使用真实素材并保存切线、独立素材和子节点', async ({ page }, testInfo) => {
  const { image } = await cc0Media(testInfo.config.configFile);
  const id = await createProject(page, '真实图片切片验证');
  const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/projects/${id}/assets`);
  await page.getByTestId('canvas-import-file').setInputFiles(image);
  expect((await uploaded).status()).toBe(201);
  const sourceId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id');
  await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.getByTestId('canvas-image-split').click();
  const dialog = page.getByTestId('image-split-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId('image-split-confirm')).toBeEnabled();
  await expect.poll(() => dialog.locator('img').evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  const line = dialog.getByTestId('image-split-vertical-line').first();
  const preview = dialog.getByTestId('image-split-preview');
  const box = await preview.boundingBox(); if (!box || !box.width || !box.height) throw new Error('真实图片预览没有显示');
  await line.dragTo(preview, { sourcePosition: { x: 8, y: box.height * .3 }, targetPosition: { x: box.width * .4, y: box.height * .3 } });
  const adjusted = await line.evaluate((element) => (element as HTMLElement).style.left);
  expect(adjusted).not.toBe('50%');
  await dialog.getByRole('button', { name: '撤销切线', exact: true }).click(); await expect(line).toHaveAttribute('style', /50%/);
  await dialog.getByRole('button', { name: '重做切线', exact: true }).click(); expect(await line.evaluate((element) => (element as HTMLElement).style.left)).toBe(adjusted);
  const splitResponse = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/media-tools/split'));
  await dialog.getByTestId('image-split-confirm').click();
  const response = await splitResponse; expect(response.status()).toBe(201);
  const result = await response.json(); expect(result.pieces).toHaveLength(4);
  await expect(dialog).toHaveCount(0); await expect(page.getByTestId('canvas-node')).toHaveCount(5);
  await page.reload();
  const persisted = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const canvas = persisted.canvases.find((item: { nodes: Array<{ id: string }> }) => item.nodes.some((node) => node.id === sourceId));
  expect(canvas.nodes).toHaveLength(5); expect(canvas.connections).toHaveLength(4);
  for (const piece of result.pieces) {
    const node = canvas.nodes.find((item: { id: string }) => item.id === piece.nodeId);
    expect(node.type).toBe('image'); expect(node.metadata.storageKey).toBe(piece.assetId);
    expect((await page.request.get(`/api/v1/assets/${piece.assetId}/content`)).status()).toBe(200);
  }
  await writeFile(testInfo.outputPath('actual-image-split.json'), JSON.stringify({ verifiedAt: new Date().toISOString(), projectId: id, sourceId, adjustedVerticalLine: adjusted, pieces: result.pieces }, null, 2));
});

test('两个真实视频经节点连线在本机拼接，成片可播放和采用', async ({ page }, testInfo) => {
  test.setTimeout(180000);
  const { video } = await cc0Media(testInfo.config.configFile);
  let confirms = 0, quotes = 0;
  page.on('request', (request) => { const url = new URL(request.url()).pathname; if (request.method() === 'POST' && /\/generations\/[^/]+\/confirm$/.test(url)) confirms++; if (request.method() === 'POST' && url.endsWith('/generations/quote')) quotes++; });
  const id = await createProject(page, '真实节点成片拼接验证');
  for (let index = 0; index < 2; index++) {
    const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/projects/${id}/assets`);
    await page.getByTestId('canvas-import-file').setInputFiles(video); expect((await uploaded).status()).toBe(201);
    await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存');
  }
  await page.getByTestId('canvas-add-config').click();
  const configId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id');
  await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存');
  const arranged = await page.evaluate(async ({ id, configId }) => {
    const envelope = await (await fetch(`/api/v1/projects/${id}`)).json(), canvas = envelope.canvases[0];
    const videos = canvas.nodes.filter((node: { type: string }) => node.type === 'video');
    const nodes = canvas.nodes.map((node: { id: string }) => ({ ...node, position: node.id === configId ? { x: 550, y: 40 } : { x: 40, y: 40 + videos.findIndex((video: { id: string }) => video.id === node.id) * 300 } }));
    const response = await fetch(`/api/v1/projects/${id}/canvases/${canvas.sceneId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nodes, connections: [], viewport: { x: 20, y: 80, k: .85 }, expectedRevision: envelope.revision, operationId: crypto.randomUUID() }) });
    return { status: response.status, videoIds: videos.map((node: { id: string }) => node.id) };
  }, { id, configId });
  expect(arranged.status).toBe(200); await page.reload(); await page.getByTestId('canvas-fit-all').click();
  const config = page.locator(`[data-testid="canvas-node"][data-node-id="${configId}"]`);
  await config.getByTestId('generation-task-join').click();
  for (const videoId of arranged.videoIds) await connectNodes(page, videoId, configId!);
  await expect(config.getByTestId('generation-join')).toBeEnabled();
  const joined = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/join'));
  await config.getByTestId('generation-join').click();
  const response = await joined; expect(response.status()).toBe(201);
  const created = (await response.json()).generation;
  expect(created.input.provider).toBe('local-ffmpeg'); expect(created.quotedCents).toBe(0);
  let generation = created;
  await expect.poll(async () => { const result = await (await page.request.get(`/api/v1/projects/${id}/generations`)).json(); generation = result.generations.find((task: { id: string }) => task.id === created.id); return generation.status; }, { timeout: 120000, intervals: [500, 1000, 2000] }).toBe('ready');
  expect(generation.actualCents).toBe(0); expect(generation.remoteTaskId).toBeNull();
  await page.reload(); await page.getByTestId('canvas-fit-all').click();
  const output = page.locator(`[data-testid="canvas-node"][data-node-id="${generation.outputNodeId}"]`);
  const player = output.getByTestId('canvas-node-body').locator('video');
  await expect(player).toBeVisible();
  const playback = await player.evaluate(async (element) => { const media = element as HTMLVideoElement; media.muted = true; await media.play(); await new Promise<void>((resolve, reject) => { const end = () => { media.pause(); media.removeEventListener('timeupdate', update); resolve(); }; const update = () => { if (media.currentTime > .1) end(); }; media.addEventListener('timeupdate', update); media.addEventListener('error', () => reject(new Error('真实成片播放失败')), { once: true }); }); return { duration: media.duration, width: media.videoWidth, height: media.videoHeight, currentTime: media.currentTime, error: media.error?.code ?? null }; });
  expect(playback.duration).toBeGreaterThan(9); expect(playback.width).toBeGreaterThan(0); expect(playback.error).toBeNull();
  await output.getByTestId('canvas-node-header').click();
  const taskCard = output.locator(`[data-task-id="${generation.id}"]`);
  await taskCard.getByText('任务详情', { exact: true }).click();
  await expect(taskCard).toContainText('本机 FFmpeg');
  await expect(taskCard).not.toContainText('NaN');
  await expect(taskCard).not.toContainText('undefined');
  await expect(taskCard.locator('ol li')).toHaveCount(2);
  const adopted = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith(`/generations/${generation.id}/adopt`));
  await output.getByTestId('generation-adopt').click(); expect((await adopted).status()).toBe(200);
  expect(confirms).toBe(0); expect(quotes).toBe(0);
  await writeFile(testInfo.outputPath('actual-local-join.json'), JSON.stringify({ verifiedAt: new Date().toISOString(), projectId: id, generationId: generation.id, assetId: generation.assetId, playback }, null, 2));
});
