import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createProjectFromList } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

async function loadSourceMedia(configFile: string) {
  const product = path.resolve(path.dirname(configFile), '..');
  const directory = path.join(product, '.intermediate/test-media');
  const temporary = path.join(product, '.intermediate/tmp');
  await mkdir(directory, { recursive: true });
  await mkdir(temporary, { recursive: true });
  const video = path.join(directory, 'flower.mp4');
  if (!existsSync(video)) {
    const response = await fetch('https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4');
    expect(response.ok).toBe(true);
    await writeFile(video, Buffer.from(await response.arrayBuffer()));
  }
  const images = [path.join(directory, 'flower-frame.png'), path.join(directory, 'flower-last-frame.png')];
  for (const [index, image] of images.entries()) {
    if (!existsSync(image)) execFileSync('ffmpeg', ['-v', 'error', '-ss', String(index * 2), '-i', video, '-frames:v', '1', '-update', '1', image], { env: { ...process.env, TMPDIR: temporary } });
  }
  return [...images, video];
}

const canvasNode = (page: Page, id: string) => page.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`);

async function connect(page: Page, sourceId: string, targetId: string) {
  const source = page.locator(`[data-testid="canvas-connection-source"][data-node-id="${sourceId}"]`);
  const target = page.locator(`[data-testid="canvas-connection-target"][data-node-id="${targetId}"]`);
  await canvasNode(page, sourceId).getByTestId('canvas-node-body').hover();
  const start = await source.boundingBox(), end = await target.boundingBox();
  if (!start || !end) throw new Error('画布节点没有显示连接端口');
  const origin = { x: start.x + start.width / 2, y: start.y + start.height / 2 };
  await page.mouse.move(origin.x, origin.y);
  await expect.poll(() => page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('button')?.getAttribute('data-testid'), origin)).toBe('canvas-connection-source');
  await page.mouse.down();
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 12 });
  await page.mouse.up();
}

test('Wan 生成器保存混合引用、首尾帧与旧视频编辑的真实输入', async ({ page }, testInfo) => {
  if (!testInfo.config.configFile) throw new Error('浏览器测试配置没有文件路径');
  const sources = await loadSourceMedia(testInfo.config.configFile);
  const submitted: string[] = [];
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && /\/generations\/[^/]+\/confirm$/.test(pathname)) submitted.push(pathname);
  });
  const account = await page.request.post('/api/v1/auth/register', { data: {
    email: `mixed-video-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID(),
  } });
  expect(account.status()).toBe(201);
  await page.goto('/zh-CN/projects');
  await createProjectFromList(page, '多参考图与视频模式验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const projectId = new URL(page.url()).searchParams.get('id');
  if (!projectId) throw new Error('验证项目没有标识');
  await page.goto(`/zh-CN/studio?id=${projectId}`);
  const addNode = page.getByTestId('canvas-add-node');
  await expect(addNode).toBeEnabled();
  for (const media of sources) {
    const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/assets'));
    await page.getByTestId('canvas-import-file').setInputFiles(media);
    expect((await uploaded).status()).toBe(201);
    await page.getByTestId('canvas-save').click();
    await expect(page.getByTestId('save-status')).toContainText('已保存');
  }
  const createMenu = page.getByTestId('canvas-node-create-menu');
  await addNode.click();
  await createMenu.getByTestId('canvas-create-text').click();
  await addNode.click();
  await createMenu.getByTestId('canvas-create-video').click();
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const project = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
  const storedCanvas = project.canvases[0];
  const [opening, closing, originalVideo, script, generator] = storedCanvas.nodes;
  const scriptText = '前三秒保持镜头位置，随后花瓣随风轻轻摆动。';
  const leftX = 42, topY = 55;
  const middleX = leftX + Math.max(opening.width, closing.width) + 160;
  const rightX = middleX + Math.max(script.width, originalVideo.width) + 160;
  const lowerY = topY + Math.max(opening.height, script.height) + 100;
  const arranged = await page.request.put(`/api/v1/projects/${projectId}/canvases/${storedCanvas.sceneId}`, { data: {
    nodes: [
      { ...opening, position: { x: leftX, y: topY } },
      { ...closing, position: { x: leftX, y: lowerY } },
      { ...originalVideo, position: { x: middleX, y: lowerY } },
      { ...script, position: { x: middleX, y: topY }, metadata: { ...script.metadata, content: scriptText } },
      { ...generator, position: { x: rightX, y: 100 } },
    ],
    connections: [], viewport: { x: 25, y: 70, k: .85 }, expectedRevision: project.revision, operationId: crypto.randomUUID(),
  } });
  expect(arranged.status()).toBe(200);
  await page.reload();
  await page.getByTestId('canvas-fit-all').click();
  const sourceImage = canvasNode(page, opening.id);
  await expect(sourceImage.getByTestId('canvas-node-body')).toHaveCSS('border-radius', '16px');
  await expect(sourceImage.getByTestId('canvas-node-header').getByTestId('canvas-media-info')).toHaveText('960 × 540');
  const editor = canvasNode(page, generator.id);
  await editor.getByTestId('canvas-node-body').click();
  const config = editor.getByTestId('generation-config');
  await expect(config).toHaveCSS('background-color', 'rgb(31, 31, 31)');
  await expect(config).toHaveCSS('border-radius', '20px');
  const card = await editor.getByTestId('canvas-node-body').boundingBox();
  const panel = await config.boundingBox();
  if (!card || !panel) throw new Error('视频生成器未打开');
  expect(Math.abs(panel.width - 660)).toBeLessThan(1);
  await expect(config).toHaveCSS('min-height', '174px');
  await expect(config.getByRole('button', { name: '相机', exact: true })).toBeVisible();
  expect(Math.abs(panel.y - card.y - card.height - 16)).toBeLessThan(1);
  const footer = config.locator('.canvas-config-footer');
  await expect(footer).toHaveCSS('display', 'flex');
  await expect(config.getByRole('group', { name: '生成类别' })).toHaveCount(0);
  await expect(config.getByLabel('生成功能')).toHaveCount(0);
  await expect(config.getByTestId('generation-model-trigger')).toContainText('Wan 3.0');
  const mode = config.getByTestId('video-input-trigger');
  const options = config.getByRole('listbox', { name: '视频输入' }).getByRole('option');
  await mode.click();
  await expect(options).toHaveText(['文生视频', '全能参考', '首尾帧', '视频续写']);
  await expect(options.nth(0)).toBeEnabled();
  for (const option of [1, 2, 3]) await expect(options.nth(option)).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(mode).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(config).toHaveCount(0);
  const selectMode = async (name: string) => {
    await mode.click();
    await config.getByRole('option', { name, exact: true }).click();
  };
  for (const [index, source] of [opening, closing, originalVideo, script].entries()) {
    await test.step(`连接 ${source.title}`, async () => {
      await connect(page, source.id, generator.id);
      await expect(page.locator('[data-connection-id]')).toHaveCount(index + 1);
    });
  }
  await editor.getByTestId('canvas-node-body').click();
  await mode.click();
  await expect(options).toHaveText(['文生视频', '全能参考', '首尾帧', '视频续写']);
  for (const option of [0, 1, 2, 3]) await expect(options.nth(option)).toBeEnabled();
  await config.getByRole('option', { name: '视频续写', exact: true }).click();
  await expect(config).toHaveCSS('min-height', '244.8px');
  await expect(config.getByTestId('generation-source-video')).toBeInViewport();
  const references = editor.getByTestId('video-reference-images');
  const picked = references.getByTestId('video-reference-count');
  const referenceTrigger = config.getByTestId('generation-references-trigger');
  await referenceTrigger.click();
  await expect(picked).toContainText('已选 2/');
  await referenceTrigger.click();
  await selectMode('全能参考');
  const videoSource = config.getByTestId('generation-source-video');
  await videoSource.click();
  await editor.getByRole('combobox', { name: '参考视频', exact: true }).selectOption('');
  await expect(page.locator('[data-connection-id]')).toHaveCount(4);
  await referenceTrigger.click();
  const first = references.getByRole('checkbox', { name: `参考图 ${opening.title}`, exact: true });
  await expect(first).toBeChecked();
  await first.uncheck();
  await expect(picked).toContainText('已选 1/');
  await referenceTrigger.click();
  const prompt = editor.getByTestId('generation-prompt');
  await prompt.fill('@');
  const mentions = page.locator('[data-canvas-resource-mention-menu]');
  await expect(mentions.getByRole('button').filter({ hasText: closing.title })).toContainText('图1');
  await expect(mentions.getByRole('button').filter({ hasText: opening.title })).toHaveCount(0);
  await expect(mentions.getByRole('button').filter({ hasText: originalVideo.title })).toHaveCount(0);
  await prompt.fill('保持花瓣纹理，表现夜风中花丛的层次。');
  await expect(editor.getByTestId('generation-quote')).toBeEnabled();
  await referenceTrigger.click();
  await first.check();
  await referenceTrigger.click();
  await videoSource.click();
  await editor.getByRole('combobox', { name: '参考视频', exact: true }).selectOption(originalVideo.id);
  await prompt.fill('保持花瓣纹理，参照 @');
  const endingReference = mentions.getByRole('button').filter({ hasText: closing.title });
  await expect(endingReference).toContainText('图1');
  await endingReference.click();

  const quote = async () => {
    const received = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
    await editor.getByTestId('generation-quote').click();
    const response = await received;
    expect(response.status()).toBe(201);
    return { request: response.request().postDataJSON(), generation: (await response.json()).generation };
  };
  const mixed = await quote();
  expect(mixed.request).toMatchObject({ videoInputMode: 'reference', referenceVideoNodeId: originalVideo.id, referenceImageNodeIds: [closing.id, opening.id] });
  expect(mixed.generation).toMatchObject({ status: 'awaiting_confirmation', reservedCents: 0, remoteTaskId: null });
  const review = page.getByRole('dialog', { name: '核对视频生成' });
  await expect(review).toHaveCSS('width', '720px');
  await expect(review.getByTestId('generation-review')).toHaveAttribute('data-task-id', mixed.generation.id);
  const media = review.getByTestId('generation-media-inputs');
  await expect(media.getByRole('img', { name: '图1', exact: true })).toHaveAttribute('src', `/api/v1/assets/${closing.metadata.storageKey}/content`);
  await expect(media.getByRole('img', { name: '图2', exact: true })).toHaveAttribute('src', `/api/v1/assets/${opening.metadata.storageKey}/content`);
  await expect(media.locator('img')).toHaveCount(2);
  await expect(media.locator('img').first()).toHaveAttribute('alt', '图1');
  await expect(media.locator('video')).toHaveAttribute('src', `/api/v1/assets/${originalVideo.metadata.storageKey}/content`);
  const composed = review.getByTestId('generation-composed-input');
  await expect(composed).toContainText('【图1】');
  await expect(composed).toContainText(scriptText);
  await review.getByRole('button', { name: '返回编辑' }).click();
  await expect(review).toHaveCount(0);

  const afterQuote = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
  const saved = afterQuote.canvases.find((canvas: { sceneId: string }) => canvas.sceneId === storedCanvas.sceneId);
  const legacy = await page.request.put(`/api/v1/projects/${projectId}/canvases/${saved.sceneId}`, { data: {
    nodes: saved.nodes.map((node: { id: string; metadata: Record<string, unknown> }) => node.id === generator.id
      ? { ...node, metadata: { ...node.metadata, videoSourceMode: 'edit' } } : node),
    connections: saved.connections, viewport: saved.viewport, expectedRevision: afterQuote.revision, operationId: crypto.randomUUID(),
  } });
  expect(legacy.status()).toBe(200);
  await page.reload();
  await page.getByTestId('canvas-fit-all').click();
  await editor.getByTestId('canvas-node-body').click();
  await expect(mode).toContainText('视频编辑');
  await mode.click();
  await expect(options).toHaveText(['文生视频', '全能参考', '首尾帧', '视频续写']);
  await page.keyboard.press('Escape');
  await videoSource.click();
  await expect(editor.getByRole('combobox', { name: '原视频', exact: true })).toHaveValue(originalVideo.id);
  await videoSource.click();
  const previous = await quote();
  expect(previous.request).toMatchObject({ videoInputMode: 'edit', referenceVideoNodeId: originalVideo.id, referenceImageNodeIds: [closing.id, opening.id] });
  expect(previous.generation).toMatchObject({ status: 'awaiting_confirmation', reservedCents: 0, remoteTaskId: null });
  await review.getByRole('button', { name: '返回编辑' }).click();
  await expect(review).toHaveCount(0);

  await selectMode('首尾帧');
  await expect(references).toHaveCount(0);
  await config.getByTestId('generation-source-first').click();
  await expect(editor.getByRole('combobox', { name: '首帧', exact: true })).toHaveValue(opening.id);
  await config.getByTestId('generation-source-last').click();
  await editor.getByRole('combobox', { name: '尾帧', exact: true }).selectOption(closing.id);
  await prompt.fill('按镜头文本保留画面的花瓣纹理。');
  const frames = await quote();
  expect(frames.request).toMatchObject({ firstFrameNodeId: opening.id, lastFrameNodeId: closing.id });
  expect(frames.request).not.toHaveProperty('referenceImageNodeIds');
  expect(frames.request).not.toHaveProperty('referenceVideoNodeId');
  expect(frames.generation.remoteTaskId).toBeNull();
  expect(submitted).toEqual([]);
});
