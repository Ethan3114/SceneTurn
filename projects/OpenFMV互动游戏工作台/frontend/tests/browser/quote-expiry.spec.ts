import { expect, test, type Locator } from '@playwright/test';
import { addCanvasNode, createProjectFromList, enterManualStudio } from './manual-studio';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';

test.use({ viewport: { width: 1440, height: 1000 } });

async function renderedViewport(node: Locator) {
  return node.evaluate((element) => {
    for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
      const transform = getComputedStyle(ancestor).transform;
      if (transform !== 'none') {
        const matrix = new DOMMatrixReadOnly(transform);
        return { x: matrix.e, y: matrix.f, k: matrix.a };
      }
    }
    throw new Error('实际画布没有显示视口变换');
  });
}

test('过期报价和未保存编辑分别提示，重新报价始终等待用户确认', async ({ page }, info) => {
  let confirmRequests = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && /^\/api\/v1\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname)) confirmRequests += 1;
  });
  const email = `quote-expiry-${crypto.randomUUID()}@example.com`;
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(email);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '报价失效与重新报价验证');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  await addCanvasNode(page, 'text');
  const textId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id');
  expect(textId).toBeTruthy();
  const textNode = page.locator(`[data-testid="canvas-node"][data-node-id="${textId}"]`);
  await textNode.getByTestId('canvas-node-body').dblclick();
  await textNode.getByTestId('canvas-text').fill('镜头验证输入');
  await addCanvasNode(page, 'video');
  const sourceId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id');
  expect(sourceId).toBeTruthy();
  const sourceNode = page.locator(`[data-testid="canvas-node"][data-node-id="${sourceId}"]`);
  const header = await sourceNode.getByTestId('canvas-node-header').boundingBox();
  expect(header).not.toBeNull();
  if (!header) throw new Error('视频生成节点标题栏未显示');
  await page.mouse.move(header.x + 80, header.y + 12);
  await page.mouse.down();
  await page.mouse.move(header.x + 80, header.y + 72, { steps: 12 });
  await page.mouse.up();
  await textNode.getByTestId('canvas-node-body').hover();
  await page.locator(`[data-testid="canvas-connection-source"][data-node-id="${textId}"]`).hover();
  const outputHandle = await page.locator(`[data-testid="canvas-connection-source"][data-node-id="${textId}"]`).boundingBox();
  const inputHandle = await page.locator(`[data-testid="canvas-connection-target"][data-node-id="${sourceId}"]`).boundingBox();
  expect(outputHandle).not.toBeNull();
  expect(inputHandle).not.toBeNull();
  if (!outputHandle || !inputHandle) throw new Error('实际节点连接端点未显示');
  await page.mouse.move(outputHandle.x + outputHandle.width / 2, outputHandle.y + outputHandle.height / 2);
  await page.mouse.down();
  await page.mouse.move(inputHandle.x + inputHandle.width / 2, inputHandle.y + inputHandle.height / 2, { steps: 12 });
  await page.mouse.up();
  await sourceNode.getByTestId('canvas-node-header').click();
  await expect(sourceNode.locator('[data-node-panel]').getByTestId('generation-prompt')).toBeVisible();
  await expect(sourceNode.getByTestId('canvas-node-body').getByTestId('generation-prompt')).toHaveCount(0);
  await sourceNode.getByTestId('generation-prompt').fill(`清晨的空旷车站，固定镜头，阳光照进候车大厅。参考@[node:${textId}]。`);
  const firstQuoteResponse = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
  await sourceNode.getByTestId('generation-quote').click();
  const first = (await (await firstQuoteResponse).json()).generation as { id: string; projectRevision: number; reservedCents: number; sourceNodeId: string; outputNodeId: string; input: { sourcePrompt: string; prompt: string; inputs: Array<{ nodeId: string }> } };
  expect(first.sourceNodeId).toBe(sourceId);
  expect(first.outputNodeId).toBeTruthy();
  expect(first.outputNodeId).not.toBe(sourceId);
  await expect(page.locator(`[data-testid="canvas-node"][data-node-id="${first.outputNodeId}"]`)).toHaveCount(1);
  expect(first.input.sourcePrompt).toContain('清晨');
  expect(first.input.sourcePrompt).toContain(`@[node:${textId}]`);
  expect(first.input.prompt).toContain('【文本1】');
  expect(first.input.prompt).toContain('镜头验证输入');
  expect(first.input.inputs.map((input) => input.nodeId)).toEqual([textId]);
  const review = page.getByRole('dialog', { name: '核对视频生成' });
  await expect(review.getByTestId('generation-confirm')).toBeEnabled();
  await review.getByRole('button', { name: '返回编辑' }).click();
  const oldCard = page.locator(`[data-task-id="${first.id}"]`);
  await expect(oldCard.getByTestId('generation-open-review')).toBeVisible();
  expect(first.reservedCents).toBe(0);
  const initialViewport = await renderedViewport(sourceNode);

  const surface = page.getByTestId('canvas-surface');
  const background = await surface.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const isBlank = (x: number, y: number) => {
      const target = document.elementFromPoint(x, y);
      return target && element.contains(target) && !target.closest('[data-node-id],[data-connection-id],[data-canvas-no-zoom]');
    };
    for (const offsetY of [80, 160, bounds.height / 2, bounds.height - 88]) {
      for (const offsetX of [bounds.width - 45, bounds.width - 165, bounds.width / 2, 160]) {
        const point = { x: bounds.x + offsetX, y: bounds.y + offsetY };
        if (isBlank(point.x, point.y) && isBlank(point.x - 70, point.y - 40)) return point;
      }
    }
    throw new Error('实际画布没有可平移的空白区域');
  });
  await page.keyboard.down('Space');
  await page.mouse.move(background.x, background.y);
  await page.mouse.down();
  await page.mouse.move(background.x - 70, background.y - 40, { steps: 10 });
  await page.mouse.up();
  await page.keyboard.up('Space');
  await page.mouse.move(background.x - 70, background.y - 40);
  await page.mouse.wheel(0, -100);
  const editedViewport = await renderedViewport(sourceNode);
  expect(editedViewport.k).not.toBe(initialViewport.k);
  expect(editedViewport.x).not.toBe(initialViewport.x);
  expect(editedViewport.y).not.toBe(initialViewport.y);
  await expect(page.getByTestId('save-status')).toHaveText('视口有未保存的调整');
  await oldCard.getByTestId('generation-open-review').click();
  await expect(review.getByTestId('quote-unsaved-message')).toHaveCount(0);
  await expect(review.getByTestId('generation-confirm')).toBeEnabled();
  await review.getByRole('button', { name: '返回编辑' }).click();
  expect(confirmRequests).toBe(0);
  const beforeReload = await sourceNode.boundingBox();
  const beforeSurface = await surface.boundingBox();
  expect(beforeReload).not.toBeNull();

  const savedResponse = page.waitForResponse((response) => response.request().method() === 'PUT' && new URL(response.url()).pathname.includes('/canvases/'));
  await page.getByTestId('canvas-save').click();
  const saved = await (await savedResponse).json() as { revision: number; canvas: { viewport: { x: number; y: number; k: number } } };
  expect(saved.revision).toBeGreaterThan(first.projectRevision);
  expect(saved.canvas.viewport.k).toBeCloseTo(editedViewport.k, 4);
  expect(saved.canvas.viewport.x).toBeCloseTo(editedViewport.x, 3);
  expect(saved.canvas.viewport.y).toBeCloseTo(editedViewport.y, 3);
  await page.reload();
  const opened = await page.request.get(`/api/v1/projects/${projectId}`);
  expect(opened.ok()).toBe(true);
  const restored = await opened.json() as { revision: number; canvases: Array<{ nodes: Array<{ id: string }>; viewport: { x: number; y: number; k: number } }> };
  expect(restored.revision).toBe(saved.revision);
  expect(restored.canvases.find((canvas) => canvas.nodes.some((node) => node.id === sourceId))?.viewport).toEqual(saved.canvas.viewport);
  const afterReload = await sourceNode.boundingBox();
  const afterSurface = await surface.boundingBox();
  if (!beforeReload || !afterReload || !beforeSurface || !afterSurface) throw new Error('刷新前后的来源节点或画布未显示');
  await writeFile(info.outputPath('actual-viewport-restoration.json'), JSON.stringify({ beforeReload, afterReload, beforeSurface, afterSurface, savedViewport: saved.canvas.viewport, renderedViewport: await renderedViewport(sourceNode) }, null, 2));
  expect(Math.abs((afterReload.x - afterSurface.x) - (beforeReload.x - beforeSurface.x))).toBeLessThan(2);
  expect(Math.abs((afterReload.y - afterSurface.y) - (beforeReload.y - beforeSurface.y))).toBeLessThan(2);
  expect(Math.abs(afterReload.width - beforeReload.width)).toBeLessThan(2);
  await sourceNode.getByTestId('canvas-node-header').click();
  await expect(oldCard).toHaveAttribute('data-quote-state', 'expired');
  await expect(oldCard.getByRole('heading', { name: '生成输入需要重新核对' })).toBeVisible();
  await oldCard.getByTestId('generation-open-review').click();
  await expect(review.getByTestId('quote-expired-message')).toContainText(`版本 ${first.projectRevision}`);
  await expect(review.getByTestId('quote-expired-message')).toContainText(`版本 ${saved.revision}`);
  await expect(review.getByTestId('generation-confirm')).toHaveCount(0);
  await expect(review.getByTestId('quote-unsaved-message')).toHaveCount(0);

  const nextQuoteResponse = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
  const nextSaveResponse = page.waitForResponse((response) => response.request().method() === 'PUT' && new URL(response.url()).pathname.includes('/canvases/'));
  await review.getByRole('button', { name: '按当前输入重新核对', exact: true }).click();
  const second = (await (await nextQuoteResponse).json()).generation as { id: string; projectRevision: number; status: string; reservedCents: number; outputNodeId: string };
  const nextSaved = await (await nextSaveResponse).json() as { revision: number };
  expect(second.id).not.toBe(first.id);
  expect(nextSaved.revision).toBeGreaterThan(saved.revision);
  expect(second.projectRevision).toBe(nextSaved.revision);
  expect(second.outputNodeId).not.toBe(first.outputNodeId);
  expect(second.status).toBe('awaiting_confirmation');
  expect(second.reservedCents).toBe(0);
  const newCard = page.locator(`[data-task-id="${second.id}"]`);
  await expect(review.getByTestId('generation-review')).toHaveAttribute('data-task-id', second.id);
  await expect(review.getByTestId('generation-confirm')).toBeVisible();
  await review.getByRole('button', { name: '返回编辑' }).click();
  await expect(oldCard).toBeVisible();

  await sourceNode.getByTestId('generation-prompt').fill('傍晚的空旷车站，固定镜头，灯光照亮候车大厅。');
  await expect(newCard).toHaveAttribute('data-quote-state', 'unsaved');
  await newCard.getByTestId('generation-open-review').click();
  await expect(review.getByTestId('quote-unsaved-message')).toBeVisible();
  await expect(review.getByTestId('quote-expired-message')).toHaveCount(0);
  await expect(review.getByTestId('generation-confirm')).toHaveCount(0);
  const finalQuoteResponse = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
  await review.getByRole('button', { name: '保存当前输入并重新核对', exact: true }).click();
  const third = (await (await finalQuoteResponse).json()).generation as { id: string; status: string; reservedCents: number; input: { prompt: string } };
  expect(third.input.prompt).toContain('傍晚');
  expect(third.status).toBe('awaiting_confirmation');
  expect(third.reservedCents).toBe(0);
  await expect(review.getByTestId('generation-review')).toHaveAttribute('data-task-id', third.id);
  await expect(review.getByTestId('generation-confirm')).toBeVisible();
  await review.getByRole('button', { name: '返回编辑' }).click();
  const response = await page.request.get(`/api/v1/projects/${projectId}/generations`);
  expect(response.ok()).toBe(true);
  const persisted = await response.json() as { generations: Array<{ id: string; status: string; reservedCents: number }> };
  expect(persisted.generations.map((generation) => generation.id)).toEqual(expect.arrayContaining([first.id, second.id, third.id]));
  expect(persisted.generations.every((generation) => generation.status === 'awaiting_confirmation' && generation.reservedCents === 0)).toBe(true);
  expect(confirmRequests).toBe(0);
  await page.getByTestId('logout').click();
  await expect(page).toHaveURL(/\/account$/);
});

test('来源视频节点展示正式接口创建的待确认报价', async ({ page }) => {
  let confirmRequests = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && /^\/api\/v1\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname)) confirmRequests += 1;
  });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`source-layout-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '来源视频节点待确认布局验证');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  await addCanvasNode(page, 'video');
  const sourceId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id');
  expect(sourceId).toBeTruthy();
  const sourceNode = page.locator(`[data-testid="canvas-node"][data-node-id="${sourceId}"]`);
  await sourceNode.locator('[data-node-panel]').getByTestId('generation-prompt').fill('树林中的清晨，固定镜头，光线自然。');
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const stored = await page.request.get(`/api/v1/projects/${projectId}`);
  expect(stored.ok()).toBe(true);
  const envelope = await stored.json() as { canvases: Array<{ sceneId: string; nodes: Array<{ id: string; metadata: { composerContent?: string; prompt?: string } }> }> };
  const canvas = envelope.canvases.find((item) => item.nodes.some((node) => node.id === sourceId));
  const source = canvas?.nodes.find((node) => node.id === sourceId);
  if (!canvas || !source) throw new Error('真实保存结果缺少来源视频节点');
  const prompt = source.metadata.composerContent ?? source.metadata.prompt;
  expect(prompt).toBe('树林中的清晨，固定镜头，光线自然。');
  const quoted = await page.evaluate(async ({ id, sceneId, sourceNodeId, text }) => {
    const response = await fetch(`/api/v1/projects/${id}/generations/quote`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sceneId, sourceNodeId, prompt: text, duration: 5, resolution: '720P', operationId: crypto.randomUUID() }),
    });
    return { status: response.status, body: await response.json() };
  }, { id: projectId, sceneId: canvas.sceneId, sourceNodeId: sourceId, text: prompt });
  expect(quoted.status).toBe(201);
  const generation = quoted.body.generation as { id: string; sourceNodeId: string; outputNodeId: string | null; status: string; reservedCents: number; remoteTaskId: string | null };
  expect(generation.sourceNodeId).toBe(sourceId);
  expect(generation.outputNodeId).toBeNull();
  expect(generation.status).toBe('awaiting_confirmation');
  expect(generation.reservedCents).toBe(0);
  expect(generation.remoteTaskId).toBeNull();
  await page.reload();
  await sourceNode.getByTestId('canvas-node-header').click();
  const task = sourceNode.locator(`[data-task-id="${generation.id}"]`);
  await expect(task).toBeVisible();
  await task.getByTestId('generation-open-review').click();
  const review = page.getByRole('dialog', { name: '核对视频生成' });
  await expect(review.getByTestId('generation-confirm')).toBeEnabled();
  await review.getByRole('button', { name: '返回编辑' }).click();
  const panel = sourceNode.locator('[data-node-panel]');
  await expect(panel.getByTestId('generation-config')).toBeVisible();
  await expect(panel.getByTestId('generation-prompt')).toContainText(prompt!);
  await panel.getByTestId('generation-model-trigger').click();
  await expect(panel.getByRole('listbox', { name: '模型' })).toBeVisible();
  await expect(sourceNode.getByTestId('generation-create-config')).toHaveCount(0);
  await expect(page.getByTestId('canvas-node')).toHaveCount(1);
  expect(confirmRequests).toBe(0);
  await page.getByTestId('logout').click();
  await expect(page).toHaveURL(/\/account$/);
});

test('未连接的显式引用在节点内阻止报价和快捷提交', async ({ page }) => {
  let quoteRequests = 0;
  let confirmRequests = 0;
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && pathname.endsWith('/generations/quote')) quoteRequests += 1;
    if (request.method() === 'POST' && /^\/api\/v1\/generations\/[^/]+\/confirm$/.test(pathname)) confirmRequests += 1;
  });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`invalid-reference-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '节点显式引用前置校验');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  await addCanvasNode(page, 'video');
  const source = page.getByTestId('canvas-node').last();
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const before = await (await page.request.get(`/api/v1/projects/${projectId}`)).json() as { revision: number };
  const count = await page.getByTestId('canvas-node').count();
  const prompt = source.getByTestId('generation-prompt');
  await prompt.fill('参考@[node:missing-reference]生成清晨车站镜头。');
  await expect(source.getByTestId('generation-input-problem')).toBeVisible();
  await expect(source.getByTestId('generation-quote')).toBeDisabled();
  await prompt.press('ControlOrMeta+Enter');
  expect(await page.getByTestId('canvas-node').count()).toBe(count);
  const after = await (await page.request.get(`/api/v1/projects/${projectId}`)).json() as { revision: number };
  expect(after.revision).toBe(before.revision);
  expect(quoteRequests).toBe(0);
  expect(confirmRequests).toBe(0);
});

test('组合提示词超过20000字符时保留节点且不增加服务器版本', async ({ page }) => {
  let quoteRequests = 0;
  let confirmRequests = 0;
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && pathname.endsWith('/generations/quote')) quoteRequests += 1;
    if (request.method() === 'POST' && /^\/api\/v1\/generations\/[^/]+\/confirm$/.test(pathname)) confirmRequests += 1;
  });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`long-composition-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '节点组合长度前置校验');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  const authored = await page.evaluate(async (id) => {
    const project = await (await fetch(`/api/v1/projects/${id}`)).json();
    const sceneId = project.project.graphData.nodes.find((node: { id: string; type: string }) => node.type === 'scene').id;
    const textId = crypto.randomUUID();
    const sourceId = crypto.randomUUID();
    const response = await fetch(`/api/v1/projects/${id}/canvases/${sceneId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expectedRevision: project.revision, operationId: crypto.randomUUID(),
        nodes: [
          { id: textId, type: 'text', title: '真实边界文字', position: { x: 80, y: 80 }, width: 300, height: 220, metadata: { content: '甲'.repeat(19990) } },
          { id: sourceId, type: 'config', title: '视频生成配置', position: { x: 480, y: 80 }, width: 340, height: 240, metadata: { generationMode: 'video', composerContent: '指令', seconds: '5', vquality: '720P' } },
        ],
        connections: [{ id: crypto.randomUUID(), fromNodeId: textId, toNodeId: sourceId }], viewport: { x: 20, y: 80, k: 0.7 },
      }),
    });
    return { status: response.status, saved: await response.json(), textId, sourceId };
  }, projectId);
  expect(authored.status).toBe(200);
  await page.reload();
  const source = page.locator(`[data-testid="canvas-node"][data-node-id="${authored.sourceId}"]`);
  const text = page.locator(`[data-testid="canvas-node"][data-node-id="${authored.textId}"]`);
  await source.getByTestId('canvas-node-header').click();
  await expect(source.getByTestId('generation-input-problem')).toHaveCount(0);
  await expect(source.getByTestId('generation-quote')).toBeEnabled();
  const count = await page.getByTestId('canvas-node').count();
  await text.getByTestId('canvas-node-body').dblclick();
  await text.getByTestId('canvas-text').fill('甲'.repeat(19991));
  await source.getByTestId('canvas-node-header').click();
  await expect(source.getByTestId('generation-input-problem')).toContainText('20000');
  await expect(source.getByTestId('generation-quote')).toBeDisabled();
  await source.getByTestId('generation-prompt').press('ControlOrMeta+Enter');
  expect(await page.getByTestId('canvas-node').count()).toBe(count);
  const after = await (await page.request.get(`/api/v1/projects/${projectId}`)).json() as { revision: number };
  expect(after.revision).toBe(authored.saved.revision);
  expect(quoteRequests).toBe(0);
  expect(confirmRequests).toBe(0);
});

test('组合复制和取消组合保存真实内部连接、引用及素材身份', async ({ page }, testInfo) => {
  let confirmRequests = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && /^\/api\/v1\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname)) confirmRequests += 1;
  });
  if (!testInfo.config.configFile) throw new Error('真实浏览器配置路径不可用');
  const mediaFile = path.resolve(path.dirname(testInfo.config.configFile), '../.intermediate/test-media/flower.mp4');
  if (!existsSync(mediaFile)) {
    await mkdir(path.dirname(mediaFile), { recursive: true });
    const response = await fetch('https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4');
    expect(response.ok).toBe(true);
    await writeFile(mediaFile, Buffer.from(await response.arrayBuffer()));
  }
  expect((await readFile(mediaFile)).byteLength).toBeGreaterThan(0);
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`group-copy-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '组合与复制真实保存验证');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  const uploadResponse = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/projects/${projectId}/assets`);
  await page.getByTestId('canvas-import-file').setInputFiles(mediaFile);
  const uploaded = await uploadResponse;
  expect(uploaded.status()).toBe(201);
  const asset = (await uploaded.json()).asset as { id: string; generationId: string | null };
  expect(asset.generationId).toBeNull();
  await expect(page.getByTestId('canvas-node').locator('video')).toBeVisible();
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const authored = await page.evaluate(async ({ id, assetId }) => {
    const envelope = await (await fetch(`/api/v1/projects/${id}`)).json();
    const canvas = envelope.canvases[0];
    const media = canvas.nodes.find((node: { metadata?: { storageKey?: string } }) => node.metadata?.storageKey === assetId);
    if (!media) throw new Error('真实导入素材没有对应画布节点');
    const textId = crypto.randomUUID(), sourceId = crypto.randomUUID(), outputId = crypto.randomUUID();
    const composer = `根据@[node:${textId}]生成视频。`;
    const saved = await fetch(`/api/v1/projects/${id}/canvases/${canvas.sceneId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expectedRevision: envelope.revision, operationId: crypto.randomUUID(),
        nodes: [
          { id: textId, type: 'text', title: '组合文字', position: { x: 80, y: 80 }, width: 300, height: 220, metadata: { content: '真实组合引用的树林镜头。' } },
          { id: sourceId, type: 'config', title: '组合配置', position: { x: 480, y: 80 }, width: 340, height: 240, metadata: { generationMode: 'video', composerContent: composer, outputNodeId: outputId, seconds: '5', vquality: '720P' } },
          { id: outputId, type: 'video', title: '待确认结果', position: { x: 900, y: 80 }, width: 320, height: 180, metadata: { status: 'idle', pendingResult: true } },
          { ...media, position: { x: 80, y: 400 } },
        ],
        connections: [{ id: crypto.randomUUID(), fromNodeId: textId, toNodeId: sourceId }, { id: crypto.randomUUID(), fromNodeId: sourceId, toNodeId: outputId }],
        viewport: { x: 20, y: 50, k: 0.65 },
      }),
    });
    if (!saved.ok) throw new Error('真实组合测试画布保存失败');
    const quote = await fetch(`/api/v1/projects/${id}/generations/quote`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sceneId: canvas.sceneId, sourceNodeId: sourceId, outputNodeId: outputId, prompt: composer, duration: 5, resolution: '720P', operationId: crypto.randomUUID() }),
    });
    return { status: quote.status, generation: (await quote.json()).generation, sceneId: canvas.sceneId, textId, sourceId, outputId, mediaId: media.id };
  }, { id: projectId, assetId: asset.id });
  expect(authored.status).toBe(201);
  expect(authored.generation.status).toBe('awaiting_confirmation');
  expect(authored.generation.reservedCents).toBe(0);
  expect(authored.generation.remoteTaskId).toBeNull();
  await page.reload();
  await page.getByTestId('canvas-fit-all').click();
  for (const [index, id] of [authored.textId, authored.sourceId, authored.outputId, authored.mediaId].entries()) {
    await page.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`).getByTestId('canvas-node-header').click({ modifiers: index === 0 ? [] : ['Shift'] });
  }
  await page.getByTestId('canvas-group-selection').click();
  await expect(page.getByTestId('canvas-node')).toHaveCount(5);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  type StoredNode = { id: string; type: string; title: string; metadata: Record<string, unknown> };
  type StoredCanvas = { sceneId: string; nodes: StoredNode[]; connections: Array<{ fromNodeId: string; toNodeId: string }> };
  const loadCanvas = async () => {
    const response = await page.request.get(`/api/v1/projects/${projectId}`);
    expect(response.ok()).toBe(true);
    const envelope = await response.json() as { canvases: StoredCanvas[] };
    const canvas = envelope.canvases.find((item) => item.sceneId === authored.sceneId);
    if (!canvas) throw new Error('正式服务缺少组合画布');
    return canvas;
  };
  const grouped = await loadCanvas();
  const originalGroup = grouped.nodes.find((node) => node.type === 'group');
  if (!originalGroup) throw new Error('实际多选没有创建Group');
  expect(grouped.nodes.filter((node) => node.metadata.groupId === originalGroup.id)).toHaveLength(4);
  await page.getByTestId('canvas-copy-selection').click();
  await expect(page.getByTestId('canvas-node')).toHaveCount(10);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.reload();
  const copied = await loadCanvas();
  const originalIds = new Set(grouped.nodes.map((node) => node.id));
  const copies = copied.nodes.filter((node) => !originalIds.has(node.id));
  const copyGroup = copies.find((node) => node.type === 'group');
  const copyText = copies.find((node) => node.type === 'text');
  const copyConfig = copies.find((node) => node.type === 'config');
  const copyMedia = copies.find((node) => node.type === 'video' && node.metadata.storageKey === asset.id);
  const copyOutput = copies.find((node) => node.type === 'video' && !node.metadata.storageKey);
  if (!copyGroup || !copyText || !copyConfig || !copyMedia || !copyOutput) throw new Error('真实Group复制缺少直接成员');
  expect(copies.filter((node) => node.metadata.groupId === copyGroup.id)).toHaveLength(4);
  expect(copyConfig.metadata.composerContent).toBe(`根据@[node:${copyText.id}]生成视频。`);
  expect(copyConfig.metadata.outputNodeId).toBe(copyOutput.id);
  expect(copyMedia.metadata.storageKey).toBe(asset.id);
  expect(copyOutput.metadata.pendingResult).toBe(false);
  expect(copies.every((node) => node.metadata.videoTaskId === undefined && node.metadata.videoTaskProvider === undefined)).toBe(true);
  expect(copied.connections.filter((edge) => edge.fromNodeId === copyText.id && edge.toNodeId === copyConfig.id)).toHaveLength(1);
  expect(copied.connections.filter((edge) => edge.fromNodeId === copyConfig.id && edge.toNodeId === copyOutput.id)).toHaveLength(1);
  const tasks = await (await page.request.get(`/api/v1/projects/${projectId}/generations`)).json() as { generations: Array<{ id: string }> };
  expect(tasks.generations.map((task) => task.id)).toEqual([authored.generation.id]);
  await page.getByTestId('canvas-fit-all').click();
  await page.locator(`[data-testid="canvas-node"][data-node-id="${copyGroup.id}"]`).getByTestId('canvas-node-header').click();
  await page.getByTestId('canvas-ungroup-selection').click();
  await expect(page.getByTestId('canvas-node')).toHaveCount(9);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.reload();
  const restored = await loadCanvas();
  expect(restored.nodes).toHaveLength(9);
  expect(restored.nodes.some((node) => node.id === originalGroup.id)).toBe(true);
  expect(restored.nodes.some((node) => node.id === copyGroup.id)).toBe(false);
  expect(restored.nodes.filter((node) => [copyText.id, copyConfig.id, copyOutput.id, copyMedia.id].includes(node.id)).every((node) => node.metadata.groupId === undefined)).toBe(true);
  expect(restored.connections).toHaveLength(4);
  expect(restored.nodes.find((node) => node.id === copyMedia.id)?.metadata.storageKey).toBe(asset.id);
  expect(confirmRequests).toBe(0);
});
