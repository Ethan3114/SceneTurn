import path from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { createProjectFromList } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

type Point = { x: number; y: number };

async function openManualCanvas(page: Page) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`canvas-review-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '素材画布操作复测');
  await expect(page).toHaveURL(/\/editor\?id=/);
  await page.getByTitle('素材', { exact: true }).click();
  await page.getByTestId('materials-generate-tab').click();
  await page.getByRole('button', { name: '关闭素材对话' }).click();
  const workspace = page.getByTestId('material-workspace');
  await workspace.getByRole('button', { name: '手动编辑画布' }).click();
  const canvas = workspace.getByTestId('embedded-material-canvas');
  await expect(canvas.getByTestId('canvas-add-node')).toBeEnabled();
  return canvas;
}

async function nodeId(node: Locator) {
  const id = await node.getAttribute('data-node-id');
  if (!id) throw new Error('画布节点没有标识');
  return id;
}

async function addNode(canvas: Locator, kind: 'image' | 'video' | 'text') {
  const nodes = canvas.getByTestId('canvas-node');
  const before = await nodes.count();
  await canvas.getByTestId('canvas-add-node').click();
  await canvas.getByTestId(`canvas-create-${kind}`).click();
  await expect(nodes).toHaveCount(before + 1);
  return canvas.locator(`[data-testid="canvas-node"][data-node-id="${await nodeId(nodes.last())}"]`);
}

function port(canvas: Locator, id: string, side: 'source' | 'target') {
  return canvas.locator(`[data-testid="canvas-connection-${side}"][data-node-id="${id}"]`);
}

async function box(locator: Locator) {
  const bounds = await locator.boundingBox();
  if (!bounds) throw new Error('画布操作对象没有显示');
  return bounds;
}

async function center(locator: Locator) {
  const { x, y, width, height } = await box(locator);
  return { x: x + width / 2, y: y + height / 2 };
}

async function canvasPoint(locator: Locator, blank: boolean) {
  return locator.evaluate((element, blank) => {
    const bounds = element.getBoundingClientRect();
    const locations = blank ? [[.8, .75], [.85, .4], [.65, .78], [.15, .75], [.18, .12]] : [[.28, .3], [.55, .36], [.5, .54], [.3, .62]];
    for (const [horizontal, vertical] of locations) {
      const x = bounds.x + bounds.width * horizontal;
      const y = bounds.y + bounds.height * vertical;
      const hit = document.elementFromPoint(x, y);
      if (!hit || !element.contains(hit) || hit.closest('[data-connection-id],[data-node-panel],button,input,video,audio')) continue;
      if (!blank || !hit.closest('[data-node-id],[data-canvas-no-zoom]')) return { x, y };
    }
    throw new Error(blank ? '画布找不到可操作的空白位置' : '卡片找不到可操作的正文位置');
  }, blank);
}

async function mouseDrag(page: Page, from: Point, to: Point, hold = 0) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  if (hold) await page.waitForTimeout(hold);
  await page.mouse.move(to.x, to.y);
  await page.mouse.up();
}

function expectShift(from: Point, to: Point, x: number, y: number) {
  expect(to.x - from.x).toBeCloseTo(x, 0);
  expect(to.y - from.y).toBeCloseTo(y, 0);
}

test('未生成的图片可以连到已有视频，已上传图片可从端口新建带参考的视频', async ({ page }) => {
  const canvas = await openManualCanvas(page);
  const video = await addNode(canvas, 'video');
  const pending = await addNode(canvas, 'image');
  const videoId = await nodeId(video);
  const pendingId = await nodeId(pending);
  const surface = canvas.getByTestId('canvas-surface');
  const edges = canvas.locator('[data-connection-id]');

  await canvas.getByTestId('canvas-fit-all').press('Enter');
  const blank = await canvasPoint(surface, true);
  await page.mouse.click(blank.x, blank.y);
  const source = port(canvas, pendingId, 'source');
  const input = port(canvas, videoId, 'target');
  await source.hover();
  await mouseDrag(page, await center(source), await center(input));
  await expect(edges).toHaveCount(1);
  await video.getByTestId('canvas-node-title').click();
  await expect(video.getByTestId('generation-source-first')).toBeVisible();
  await expect(video.getByTestId('generation-quote')).toBeDisabled();

  const imagePath = path.resolve(process.cwd(), 'public/sceneturn/canvas/sparkling-first.png');
  await canvas.getByTestId('canvas-import-file').setInputFiles(imagePath);
  const uploaded = canvas.getByRole('group', { name: 'sparkling-first.png', exact: true });
  await expect(uploaded).toBeVisible();
  const uploadedId = await nodeId(uploaded);
  await canvas.getByTestId('canvas-fit-all').press('Enter');
  await port(canvas, uploadedId, 'source').click();
  const menu = canvas.getByTestId('canvas-connection-create-menu');
  await expect(menu.getByTestId('canvas-linked-create-video')).toBeEnabled();
  await menu.getByTestId('canvas-linked-create-video').click();
  await expect(edges).toHaveCount(2);

  const linkedVideo = canvas.getByTestId('canvas-node').last();
  const linkedVideoId = await nodeId(linkedVideo);
  const config = linkedVideo.getByTestId('generation-config');
  await expect(config).toHaveAttribute('data-kind', 'video');
  await expect(config).toBeInViewport({ ratio: .1 });
  await expect(config.locator('.canvas-config-reference-strip img')).toBeVisible();
  await canvas.getByTestId('canvas-save').click();
  await expect(canvas.getByTestId('save-status')).toContainText('已保存');

  const projectId = new URL(page.url()).searchParams.get('id');
  const response = await page.request.get(`/api/v1/projects/${projectId}`);
  expect(response.ok()).toBe(true);
  const { canvases } = await response.json();
  expect(canvases[0].connections).toEqual(expect.arrayContaining([
    expect.objectContaining({ fromNodeId: pendingId, toNodeId: videoId }),
    expect.objectContaining({ fromNodeId: uploadedId, toNodeId: linkedVideoId }),
  ]));
  expect(canvases[0].nodes).toEqual(expect.arrayContaining([
    expect.objectContaining({ id: videoId, metadata: expect.objectContaining({ firstFrameNodeId: pendingId }) }),
    expect.objectContaining({ id: linkedVideoId, metadata: expect.objectContaining({ firstFrameNodeId: uploadedId }) }),
  ]));
});

test('点空白关闭图片、视频和文字设置，单击卡片正文或标题返回同一草稿', async ({ page }) => {
  const canvas = await openManualCanvas(page);
  const image = await addNode(canvas, 'image');
  const video = await addNode(canvas, 'video');
  const text = await addNode(canvas, 'text');
  const surface = canvas.getByTestId('canvas-surface');
  await canvas.getByTestId('canvas-fit-all').press('Enter');

  for (const [card, kind, draft] of [[image, 'image', '石窗透进斜阳'], [video, 'video', '镜头沿着石窗推进'], [text, 'text', '扩写石窗旁的对话']] as const) {
    const title = card.getByTestId('canvas-node-title');
    await title.click();
    const panel = card.getByTestId('generation-config');
    await expect(panel).toBeVisible();
    await expect(panel).toBeInViewport({ ratio: .1 });
    await expect(panel).toHaveAttribute('data-kind', kind);
    await panel.getByTestId('generation-prompt').fill(draft);

    const blank = await canvasPoint(surface, true);
    await page.mouse.click(blank.x, blank.y);
    await expect(panel).toHaveCount(0);
    const spot = await canvasPoint(card.getByTestId('canvas-node-body'), false);
    await page.mouse.click(spot.x, spot.y);
    await expect(panel).toBeInViewport({ ratio: .1 });
    await expect(panel.getByTestId('generation-prompt')).toHaveText(draft);
    const nextBlank = await canvasPoint(surface, true);
    await page.mouse.click(nextBlank.x, nextBlank.y);
    await expect(panel).toHaveCount(0);
    await title.click();
    await expect(panel.getByTestId('generation-prompt')).toHaveText(draft);
    await expect(card).toHaveAttribute('data-selected', 'true');
  }
});

test('直接拖动画布或卡片时平移画布，按住卡片再拖动时只移动该节点', async ({ page }) => {
  const canvas = await openManualCanvas(page);
  const first = await addNode(canvas, 'image');
  const second = await addNode(canvas, 'text');
  const surface = canvas.getByTestId('canvas-surface');
  const clearSelection = await canvasPoint(surface, true);
  await page.mouse.click(clearSelection.x, clearSelection.y);
  await expect(canvas.getByTestId('generation-config')).toHaveCount(0);
  await canvas.getByTestId('canvas-fit-all').press('Enter');

  const blank = await canvasPoint(surface, true);
  const beforeBlank = await center(first);
  await mouseDrag(page, blank, { x: blank.x + 38, y: blank.y - 26 });
  expectShift(beforeBlank, await center(first), 38, -26);

  const fromCard = await canvasPoint(first.getByTestId('canvas-node-body'), false);
  const beforeQuick = await Promise.all([center(first), center(second)]);
  await mouseDrag(page, fromCard, { x: fromCard.x + 37, y: fromCard.y + 24 });
  const afterQuick = await Promise.all([center(first), center(second)]);
  expectShift(beforeQuick[0], afterQuick[0], 37, 24);
  expectShift(beforeQuick[1], afterQuick[1], 37, 24);

  const fromHeldCard = await canvasPoint(first.getByTestId('canvas-node-body'), false);
  await mouseDrag(page, fromHeldCard, { x: fromHeldCard.x + 35, y: fromHeldCard.y + 30 }, 350);
  const afterHold = await Promise.all([center(first), center(second)]);
  expectShift(afterQuick[0], afterHold[0], 35, 30);
  expectShift(afterQuick[1], afterHold[1], 0, 0);
});
