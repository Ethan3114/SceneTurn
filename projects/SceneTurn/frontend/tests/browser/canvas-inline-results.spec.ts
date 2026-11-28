import { readFile } from 'node:fs/promises';
import path from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';

import type { CanvasConnection, CanvasNodeData } from '../../app/_features/creation-canvas/upstream/canvas';
import { createProjectFromList } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

type UploadedMedia = {
  id: string;
  url: string;
  width: number;
  height: number;
  durationMs?: number;
};

async function uploadMedia(page: Page, url: string, file: string, mimeType: string): Promise<UploadedMedia> {
  const response = await page.request.post(`${url}/assets`, {
    headers: { Origin: new URL(page.url()).origin },
    multipart: { file: { name: path.basename(file), mimeType, buffer: await readFile(file) } },
  });
  expect(response.status()).toBe(201);
  const { asset } = await response.json();
  return asset;
}

async function openMaterialCanvas(page: Page) {
  await page.getByTitle('素材', { exact: true }).click();
  await page.getByTestId('materials-generate-tab').click();
  await page.getByRole('button', { name: '关闭素材对话' }).click();
  const workspace = page.getByTestId('material-workspace');
  await workspace.getByRole('button', { name: '手动编辑画布' }).click();
  const canvas = workspace.getByTestId('embedded-material-canvas');
  await expect(canvas.getByTestId('canvas-add-node')).toBeEnabled();
  return canvas;
}

function node(canvas: Locator, id: string) {
  return canvas.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`);
}

function makeNode(type: CanvasNodeData['type'], title: string, x: number, y = 75): CanvasNodeData {
  return { id: crypto.randomUUID(), type, title, position: { x, y }, width: 350, height: 200 };
}

function connect(source: CanvasNodeData, target: CanvasNodeData): CanvasConnection {
  return { id: crypto.randomUUID(), fromNodeId: source.id, toNodeId: target.id };
}

function mediaMetadata(asset: UploadedMedia, sourceId: string): CanvasNodeData['metadata'] {
  return {
    inlineResultOf: sourceId,
    status: 'success',
    storageKey: asset.id,
    content: asset.url,
    naturalWidth: asset.width,
    naturalHeight: asset.height,
    durationMs: asset.durationMs,
  };
}

async function expectEdgeBetweenCards(canvas: Locator, edge: CanvasConnection, source: Locator, target: Locator) {
  const line = canvas.locator(`[data-connection-id="${edge.id}"]`);
  const [{ x: fromX, y: fromY }, { x: toX, y: toY }] = await line.evaluate((path: SVGPathElement) => {
    const matrix = path.getScreenCTM();
    if (!matrix) throw new Error('画布连线没有坐标');
    return [0, path.getTotalLength()].map((distance) => {
      const { x, y } = path.getPointAtLength(distance).matrixTransform(matrix);
      return { x, y };
    });
  });
  const start = await source.getByTestId('canvas-node-body').boundingBox();
  const end = await target.getByTestId('canvas-node-body').boundingBox();
  if (!start || !end) throw new Error('画布连线对应的卡片未显示');
  expect(fromX).toBeCloseTo(start.x + start.width, 0);
  expect(fromY).toBeCloseTo(start.y + start.height / 2, 0);
  expect(toX).toBeCloseTo(end.x, 0);
  expect(toY).toBeCloseTo(end.y + end.height / 2, 0);
}

test('文本、图片、视频都在原生成卡显示，保留可编辑设置及已保存的连线', async ({ page }) => {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`inline-canvas-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '生成卡片原地显示验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const editor = new URL(page.url());
  const projectId = editor.searchParams.get('id');
  if (!projectId) throw new Error('真实项目没有标识');
  const projectUrl = `/api/v1/projects/${projectId}`;
  const [previousImage, currentImage, currentVideo] = await Promise.all([
    uploadMedia(page, projectUrl, 'public/sceneturn/canvas/sparkling-first.png', 'image/png'),
    uploadMedia(page, projectUrl, 'public/sceneturn/canvas/sparkling-last.png', 'image/png'),
    uploadMedia(page, projectUrl, 'public/sceneturn/canvas/guidance/rhythm-0.mp4', 'video/mp4'),
  ]);
  const response = await page.request.get(projectUrl);
  expect(response.status()).toBe(200);
  const { canvases, revision } = await response.json();
  const canvasUrl = `${projectUrl}/canvases/${canvases[0].sceneId}`;

  const script = makeNode('text', '灯塔旁的对白', 35);
  const image = makeNode('config', '灯塔分镜', 520);
  const video = makeNode('config', '灯塔镜头', 1005);
  const scriptOutput = makeNode('text', '对白结果', 2200);
  const firstImageOutput = makeNode('image', '早期分镜结果', 2640);
  const imageOutput = makeNode('image', '当前分镜结果', 3080);
  const videoOutput = makeNode('video', '镜头结果', 3520);
  const pendingImageOutput = makeNode('image', '等待生成的分镜', 3960);
  const text = '守夜人在灯塔窗前停下，海面上的第一束光随晨雾散开。';
  const imagePrompt = '保留海雾中的灯塔轮廓';
  const changedPrompt = '保留海雾中的灯塔轮廓，降低对比度';

  script.metadata = { generationMode: 'text', dramaTask: 'text', prompt: '写一段灯塔旁的对白', outputNodeId: scriptOutput.id };
  image.metadata = { generationMode: 'image', dramaTask: 'shotImage', prompt: imagePrompt, outputNodeId: pendingImageOutput.id };
  video.metadata = { generationMode: 'video', dramaTask: 'clip', prompt: '让晨雾随风散开', outputNodeId: videoOutput.id };
  scriptOutput.metadata = { content: text, status: 'success' };
  firstImageOutput.metadata = mediaMetadata(previousImage, image.id);
  imageOutput.metadata = mediaMetadata(currentImage, image.id);
  videoOutput.metadata = mediaMetadata(currentVideo, video.id);
  pendingImageOutput.metadata = { inlineResultOf: image.id, pendingResult: true };

  const scriptLink = connect(scriptOutput, image);
  const imageLink = connect(imageOutput, video);
  const nodes = [script, image, video, scriptOutput, firstImageOutput, imageOutput, videoOutput, pendingImageOutput];
  const connections = [connect(script, scriptOutput), connect(image, firstImageOutput), connect(image, imageOutput), connect(video, videoOutput), connect(image, pendingImageOutput), scriptLink, imageLink];
  const saved = await page.request.put(canvasUrl, {
    headers: { Origin: editor.origin },
    data: { nodes, connections, expectedRevision: revision, operationId: crypto.randomUUID(), viewport: { x: 15, y: 35, k: 0.78 } },
  });
  expect(saved.status()).toBe(200);
  await page.reload();
  let canvas = await openMaterialCanvas(page);

  async function expectResults() {
    await expect(canvas.getByTestId('canvas-node')).toHaveCount(3);
    await expect(canvas.locator('[data-connection-id]')).toHaveCount(2);
    for (const output of [scriptOutput, firstImageOutput, imageOutput, videoOutput, pendingImageOutput]) {
      await expect(node(canvas, output.id)).toHaveCount(0);
    }
    await expect(node(canvas, script.id).getByTestId('canvas-node-body')).toContainText(text);
    const imageTag = node(canvas, image.id).getByTestId('canvas-node-body').locator(`img[src="${currentImage.url}"]`);
    await expect(imageTag).toBeVisible();
    await expect.poll(() => imageTag.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(currentImage.width);
    const videoTag = node(canvas, video.id).getByTestId('canvas-node-body').locator('video');
    await expect(videoTag).toHaveAttribute('src', currentVideo.url);
    await expectEdgeBetweenCards(canvas, scriptLink, node(canvas, script.id), node(canvas, image.id));
    await expectEdgeBetweenCards(canvas, imageLink, node(canvas, image.id), node(canvas, video.id));
    return videoTag;
  }

  const videoTag = await expectResults();
  await videoTag.evaluate(async (element: HTMLVideoElement) => {
    element.muted = true;
    await element.play();
  });
  await expect.poll(() => videoTag.evaluate((element: HTMLVideoElement) => element.currentTime)).toBeGreaterThan(0.1);
  await videoTag.evaluate((element: HTMLVideoElement) => element.pause());
  for (const asset of [currentImage, currentVideo]) {
    expect((await page.request.get(asset.url)).status()).toBe(200);
  }

  for (const [source, kind] of [[script, 'text'], [video, 'video'], [image, 'image']] as const) {
    const card = node(canvas, source.id);
    await card.getByTestId('canvas-node-title').click();
    const panel = card.getByTestId('generation-config');
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute('data-kind', kind);
    await expect(panel.getByTestId('generation-prompt')).toHaveText(source.metadata!.prompt!);
  }
  const imageCard = node(canvas, image.id);
  const imageBody = imageCard.getByTestId('canvas-node-body');
  await imageCard.getByRole('button', { name: '预览较旧结果' }).click();
  await expect(imageBody.locator(`img[src="${previousImage.url}"]`)).toBeVisible();
  await expect(canvas.getByTestId('canvas-node')).toHaveCount(3);
  await imageCard.getByRole('button', { name: '预览较新结果' }).click();
  await expect(imageBody.locator(`img[src="${currentImage.url}"]`)).toBeVisible();

  await imageCard.getByTestId('generation-prompt').fill(changedPrompt);
  await canvas.getByTestId('canvas-save').click();
  await expect(canvas.getByTestId('save-status')).toContainText('已保存');
  const stored = await page.request.get(canvasUrl);
  expect(stored.status()).toBe(200);
  const snapshot = (await stored.json()).canvas;
  expect(snapshot.connections).toEqual(connections);
  expect(snapshot.nodes.map(({ id, position }: CanvasNodeData) => ({ id, position })))
    .toEqual(nodes.map(({ id, position }) => ({ id, position })));
  expect(snapshot.nodes.find((item: CanvasNodeData) => item.id === image.id)?.metadata?.composerContent).toBe(changedPrompt);

  await page.reload();
  canvas = await openMaterialCanvas(page);
  await expectResults();
  const reloadedImage = node(canvas, image.id);
  await reloadedImage.getByTestId('canvas-node-title').click();
  await expect(reloadedImage.getByTestId('generation-prompt')).toHaveText(changedPrompt);
});
