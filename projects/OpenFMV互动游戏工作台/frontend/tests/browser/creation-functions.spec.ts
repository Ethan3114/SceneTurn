import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { addCanvasNode, createProjectFromList, enterManualStudio } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

async function createProject(page: Page, label: string) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`creation-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, label);
  await enterManualStudio(page);
  await expect(page.getByTestId('canvas-add-node')).toBeEnabled();
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

test('独立卡片编辑分镜，图片规格与文字计费状态保存后可恢复', async ({ page }) => {
  let paidRequests = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && (/\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname) || new URL(request.url()).pathname.endsWith('/text-generations'))) paidRequests++; });
  const projectId = await createProject(page, '节点内功能分类验证');
  const script = await addCanvasNode(page, 'script');
  const scriptId = await script.getAttribute('data-node-id');
  await expect(script.getByTestId('text-generation-start')).toBeDisabled();
  await expect(script.getByTestId('text-service-status')).toContainText(/尚未配置|尚未开放/);
  await script.getByRole('button', { name: '自己编写分镜脚本' }).click();
  const table = page.getByTestId('canvas-script-table');
  await table.getByLabel('镜头 1 名称').fill('清晨树林');
  await table.getByLabel('镜头 1 时长（秒）').fill('8');
  await table.getByTestId('canvas-script-add-row').click();
  await expect(table.getByTestId('canvas-script-row')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(table).toHaveCount(0);
  const config = await addCanvasNode(page, 'image');
  const id = await config.getAttribute('data-node-id');
  await config.getByTestId('generation-model-trigger').click();
  await expect(config.getByTestId('generation-model-codex-image')).toBeDisabled();
  await config.getByTestId('generation-specifications-trigger').click();
  await config.getByRole('button', { name: '图片比例 9:16', exact: true }).click();
  const size = config.getByRole('button', { name: '图片尺寸 4K' });
  await size.click();
  await expect(size).toHaveAttribute('title', '2304 × 4096');
  await config.getByTestId('generation-specifications-trigger').click();
  await config.getByTestId('generation-prompt').fill('清晨树林，柔和薄雾。');
  await expect(config.getByTestId('image-generation-prepare')).toBeDisabled();
  await expect(config.getByTestId('canvas-node-body').getByTestId('generation-prompt')).toHaveCount(0);
  await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.reload();
  const restored = page.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`);
  await restored.getByTestId('canvas-node-title').click();
  await expect(restored.getByTestId('generation-prompt')).toContainText('清晨树林');
  const stored = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
  expect(stored.canvases[0].nodes.find((node: { id: string }) => node.id === id).metadata).toMatchObject({ dramaTask: 'shotImage', imageWidth: 2304, imageHeight: 4096 });
  expect(stored.canvases[0].nodes.find((node: { id: string }) => node.id === scriptId).metadata.scriptRows[0]).toMatchObject({ title: '清晨树林', duration: 8 });
  await restored.getByTestId('generation-specifications-trigger').click();
  await expect(restored.getByRole('button', { name: '图片比例 9:16', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(restored.getByRole('button', { name: '图片尺寸 4K' })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '资产管理', exact: true }).click();
  const sidebar = page.getByRole('complementary', { name: '资产管理' });
  await sidebar.getByRole('searchbox', { name: '搜索节点' }).fill('图片');
  await expect(sidebar.getByRole('button', { name: '图片节点', exact: true })).toBeVisible();
  await expect(sidebar.getByRole('button', { name: '脚本生成器', exact: true })).toHaveCount(0);
  await sidebar.getByRole('button', { name: '收起节点侧栏' }).click();
  const views = page.getByRole('group', { name: '画布视图' });
  await views.getByRole('button', { name: '故事板' }).click();
  await expect(page.getByRole('region', { name: '故事板' }).getByRole('button', { name: '第一幕' })).toBeVisible();
  await views.getByRole('button', { name: '工作流' }).click();
  await expect(restored).toBeVisible();
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

test('视频节点保存首尾帧、镜头剧本和画幅，真实确认卡展示两张原图', async ({ page }, testInfo) => {
  const { image, video } = await cc0Media(testInfo.config.configFile);
  const lastImage = path.join(path.dirname(image), 'flower-last-frame.png');
  if (!existsSync(lastImage)) execFileSync('ffmpeg', ['-v', 'error', '-ss', '2', '-i', video, '-frames:v', '1', '-update', '1', lastImage], { env: { ...process.env, TMPDIR: path.join(path.dirname(testInfo.config.configFile!), '../.intermediate/tmp') } });
  const id = await createProject(page, '固定首尾帧和镜头剧本');
  for (const source of [image, lastImage]) {
    const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/assets'));
    await page.getByTestId('canvas-import-file').setInputFiles(source);
    expect((await uploaded).status()).toBe(201);
    await page.getByTestId('canvas-save').click();
    await expect(page.getByTestId('save-status')).toContainText('已保存');
  }
  await addCanvasNode(page, 'text');
  await addCanvasNode(page, 'video');
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const envelope = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const canvas = envelope.canvases[0];
  const [firstImage, endImage, script, config] = canvas.nodes;
  const positions = [{ x: 30, y: 80 }, { x: 30, y: 370 }, { x: 410, y: 80 }, { x: 800, y: 40 }];
  const placed = await page.request.put(`/api/v1/projects/${id}/canvases/${canvas.sceneId}`, { data: {
    nodes: canvas.nodes.map((node: { id: string; metadata: Record<string, unknown> }, index: number) => ({ ...node, position: positions[index], metadata: node.id === script.id ? { ...node.metadata, content: '风吹过花丛，镜头追随花瓣轻微摇动。' } : node.metadata })),
    connections: [firstImage, endImage, script].map((node) => ({ id: crypto.randomUUID(), fromNodeId: node.id, toNodeId: config.id })),
    viewport: { x: 30, y: 90, k: .85 }, expectedRevision: envelope.revision, operationId: crypto.randomUUID(),
  } });
  expect(placed.status()).toBe(200);
  await page.reload();
  const editor = page.locator(`[data-testid="canvas-node"][data-node-id="${config.id}"]`);
  await editor.getByTestId('canvas-node-title').click();
  await editor.getByTestId('video-input-trigger').click();
  await editor.getByTestId('video-mode-frames').click();
  await editor.getByTestId('generation-source-first').click();
  await editor.getByRole('combobox', { name: '首帧', exact: true }).selectOption(firstImage.id);
  await editor.getByTestId('generation-source-last').click();
  await editor.getByRole('combobox', { name: '尾帧', exact: true }).selectOption(endImage.id);
  await editor.getByTestId('generation-specifications-trigger').click();
  await editor.getByLabel('视频比例').selectOption('9:16');
  await editor.getByTestId('generation-prompt').fill('用静止镜头记录清晨。');
  await expect(editor.getByTestId('generation-input-problem')).toHaveCount(0);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');

  const saved = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const metadata = saved.canvases[0].nodes.find((node: { id: string }) => node.id === config.id).metadata;
  expect(metadata).toMatchObject({ videoSourceMode: 'frames', videoRatio: '9:16', firstFrameNodeId: firstImage.id, lastFrameNodeId: endImage.id });
  const quoted = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
  await editor.getByTestId('generation-quote').click();
  const quote = await quoted;
  expect(quote.status()).toBe(201);
  const { generation } = await quote.json();
  expect(generation.status).toBe('awaiting_confirmation');
  expect(generation.reservedCents).toBe(0);
  expect(generation.remoteTaskId).toBeNull();
  await page.reload();
  await editor.getByTestId('canvas-node-header').click();
  await editor.locator(`[data-task-id="${generation.id}"]`).getByTestId('generation-open-review').click();
  const card = page.getByRole('dialog', { name: '核对视频生成' });
  await expect(card.getByTestId('generation-composed-input')).toContainText('风吹过花丛');
  await expect(card.getByTestId('generation-media-inputs').getByRole('img', { name: '首帧' })).toHaveAttribute('src', `/api/v1/assets/${firstImage.metadata.storageKey}/content`);
  await expect(card.getByTestId('generation-media-inputs').getByRole('img', { name: '尾帧' })).toHaveAttribute('src', `/api/v1/assets/${endImage.metadata.storageKey}/content`);
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
  await addCanvasNode(page, 'smart-edit');
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
  for (const videoId of arranged.videoIds) await connectNodes(page, videoId, configId!);
  await config.getByTestId('canvas-node-title').click();
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
