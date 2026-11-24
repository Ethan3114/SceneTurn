import { expect, test, type Locator, type Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { addCanvasNode, createProjectFromList, enterManualStudio } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 }, actionTimeout: 12000 });

async function project(page: Page, title: string) {
  await page.goto('/zh-CN/account'); await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`delete-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID()); await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/); await createProjectFromList(page, title);
  await enterManualStudio(page); await expect(page.getByTestId('canvas-add-node')).toBeEnabled();
  const id = new URL(page.url()).searchParams.get('id'); if (!id) throw new Error('真实项目没有项目标识'); return id;
}
const node = (page: Page, id: string) => page.locator(`[data-testid="canvas-node"][data-node-id="${id}"]`);
async function save(page: Page) { await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存'); }
async function deselect(page: Page) { const surface = page.getByTestId('canvas-surface'), box = await surface.boundingBox(); if (!box) throw new Error('真实画布没有显示'); await surface.click({ position: { x: box.width - 8, y: box.height - 8 } }); }
async function deleteFromNodeMenu(nodeCard: Locator) {
  await nodeCard.getByTestId('canvas-node-header').hover();
  await nodeCard.getByTestId('canvas-node-more').click();
  await nodeCard.getByTestId('canvas-node-delete').click();
}

test('视频提示词与名称可编辑，删除取消保留内容，直接删除另一个节点只移除目标', async ({ page }) => {
  test.setTimeout(90000);
  const id = await project(page, '节点选中与明确删除验证');
  for (const shortcut of ['canvas-add-script', 'canvas-add-summary', 'canvas-add-plot']) await expect(page.getByTestId(shortcut)).toHaveCount(0);
  let accepted = false, dialogs = 0;
  page.on('dialog', async (dialog) => { dialogs++; if (dialog.type() !== 'confirm') { await dialog.dismiss(); throw new Error(`删除流程出现预期之外的${dialog.type()}对话框`); } if (accepted) await dialog.accept(); else await dialog.dismiss(); });
  await addCanvasNode(page, 'text');
  const textId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id'); if (!textId) throw new Error('新文字节点没有标识');
  const text = node(page, textId);
  await text.getByTestId('canvas-node-body').dblclick(); await text.getByTestId('canvas-text').fill('保留内容甲');
  await text.getByTestId('canvas-text').press('End'); await text.getByTestId('canvas-text').press('Backspace');
  for (let index = 0; index < 4; index++) await text.getByTestId('canvas-text').press('ArrowLeft');
  expect(await text.getByTestId('canvas-text').evaluate((element) => (element as HTMLTextAreaElement).selectionStart)).toBe(0);
  await text.getByTestId('canvas-text').press('Delete');
  await expect(text.getByTestId('canvas-text')).toHaveValue('留内容'); expect(dialogs).toBe(0);
  await addCanvasNode(page, 'video');
  const configId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id'); if (!configId) throw new Error('新视频节点没有标识');
  const config = node(page, configId);
  await deselect(page); await expect(config).toHaveAttribute('data-selected', 'false');
  await config.getByTestId('canvas-node-header').click();
  await expect(config).toHaveAttribute('data-selected', 'true');
  await expect(config.locator('[data-node-panel]').getByTestId('generation-config')).toHaveAttribute('data-kind', 'video');
  await config.getByTestId('generation-prompt').fill('画面甲');
  await config.getByTestId('generation-prompt').press('End'); await config.getByTestId('generation-prompt').press('Backspace');
  await expect(config.getByTestId('generation-prompt')).toHaveText('画面');
  await config.getByTestId('generation-prompt').press('ControlOrMeta+A'); await config.getByTestId('generation-prompt').press('Delete');
  await expect(config.getByTestId('generation-prompt')).toHaveText(''); expect(dialogs).toBe(0);
  await config.getByTestId('generation-prompt').fill('清晨树林，固定镜头。'); await save(page);
  await deselect(page); await config.hover(); await config.getByTestId('canvas-node-title').click();
  await expect(config).toHaveAttribute('data-selected', 'true'); await expect(config.getByTestId('canvas-node-more')).toBeVisible();
  await config.getByTestId('canvas-node-more').click();
  await config.getByRole('button', { name: '重命名' }).click();
  const title = config.getByRole('textbox', { name: '节点名称' });
  await title.fill('树林镜头');
  await title.press('Enter');
  await expect(config.getByTestId('canvas-node-title')).toHaveText('树林镜头');
  await save(page);
  await page.getByTestId('canvas-fit-all').click();
  const scrollBeforeSelection = await page.evaluate(() => ({ page: scrollY, workspace: document.querySelector('.fmv-creation-workspace')?.scrollTop }));
  await text.getByTestId('canvas-node-header').click();
  expect(await page.evaluate(() => ({ page: scrollY, workspace: document.querySelector('.fmv-creation-workspace')?.scrollTop }))).toEqual(scrollBeforeSelection);
  await expect(text).toHaveAttribute('data-selected', 'true'); await expect(config).toHaveAttribute('data-selected', 'false');
  let focusedConfig = false;
  for (let index = 0; index < 20 && !focusedConfig; index++) { await page.keyboard.press('Tab'); focusedConfig = await config.evaluate((element) => element === document.activeElement); }
  expect(focusedConfig).toBe(true); await page.keyboard.press('Delete');
  await expect(text).toHaveCount(1); await expect(config).toHaveCount(1); expect(dialogs).toBe(0);
  await text.getByTestId('canvas-node-header').click();
  const before = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  await deleteFromNodeMenu(config);
  await expect(text).toHaveCount(1); await expect(config).toHaveCount(1); expect(dialogs).toBe(1);
  const cancelled = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  expect(cancelled.revision).toBe(before.revision); expect(cancelled.canvases).toEqual(before.canvases);
  await text.getByTestId('canvas-node-header').click(); accepted = true;
  await deleteFromNodeMenu(config); await expect(config).toHaveCount(0); await expect(text).toHaveCount(1);
  await save(page); await page.reload(); await expect(config).toHaveCount(0); await expect(text).toHaveCount(1);
  const file = path.resolve(process.cwd(), '../.intermediate/test-media/flower.mp4');
  if (!existsSync(file)) { await mkdir(path.dirname(file), { recursive: true }); const response = await fetch('https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4'); expect(response.ok).toBe(true); await writeFile(file, Buffer.from(await response.arrayBuffer())); }
  const uploading = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/projects/${id}/assets`);
  await page.getByTestId('canvas-import-file').setInputFiles(file); const upload = await uploading; expect(upload.status()).toBe(201); const asset = (await upload.json()).asset;
  const importedVideo = page.getByTestId('canvas-node').filter({ has: page.locator('video[data-canvas-video]') });
  await expect(importedVideo).toHaveCount(1);
  await expect(importedVideo.getByTestId('canvas-media-info')).toContainText('960 × 540');
  await expect(importedVideo.getByTestId('canvas-media-info')).toContainText('00:05');
  const videoId = await importedVideo.getAttribute('data-node-id'); if (!videoId) throw new Error('真实视频节点没有标识');
  expect(videoId).not.toBe(textId);
  await save(page); await page.getByTestId('canvas-fit-all').click();
  const videoHeader = await node(page, videoId).getByTestId('canvas-node-header').boundingBox(); if (!videoHeader) throw new Error('真实视频标题栏没有显示');
  await page.mouse.move(videoHeader.x + 60, videoHeader.y + 8); await page.mouse.down();
  await page.waitForTimeout(350);
  await page.mouse.move(videoHeader.x + 500, videoHeader.y + 108, { steps: 12 }); await page.mouse.up();
  await page.getByTestId('canvas-fit-all').click();
  await text.getByTestId('canvas-node-header').click(); accepted = false;
  await page.keyboard.press('Delete'); await expect(text).toHaveCount(1);
  accepted = true; await page.keyboard.press('Delete'); await expect(text).toHaveCount(0);
  const video = node(page, videoId); await video.getByTestId('canvas-node-header').click(); await expect(video).toHaveAttribute('data-selected', 'true');
  await page.keyboard.press('Backspace'); await expect(video).toHaveCount(0); await save(page); await page.reload();
  await expect(page.getByTestId('canvas-node')).toHaveCount(0);
  const stored = await (await page.request.get(`/api/v1/projects/${id}`)).json(); expect(stored.project.assets.some((item: { id: string }) => item.id === asset.id)).toBe(true);
  expect((await page.request.get(`/api/v1/assets/${asset.id}/content`)).status()).toBe(200);
});

test('删除原卡片会移除待确认的结果占位，任务与冻结输入保留', async ({ page }) => {
  const id = await project(page, '删除节点后任务历史保留验证');
  let confirms = 0;
  page.on('request', (request) => { if (request.method() === 'POST' && /\/generations\/[^/]+\/confirm$/.test(new URL(request.url()).pathname)) confirms++; });
  page.on('dialog', (dialog) => dialog.accept());
  await addCanvasNode(page, 'video');
  const sourceId = await page.getByTestId('canvas-node').last().getAttribute('data-node-id'); if (!sourceId) throw new Error('真实来源节点没有标识');
  const source = node(page, sourceId); await source.getByTestId('generation-prompt').fill('清晨树林，阳光穿过树叶，固定镜头。');
  const quote = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/generations/quote'));
  await source.getByTestId('generation-quote').click(); const response = await quote; expect(response.status()).toBe(201); const task = (await response.json()).generation;
  expect(task.status).toBe('awaiting_confirmation'); expect(task.reservedCents).toBe(0); expect(task.remoteTaskId).toBeNull();
  await page.getByRole('button', { name: '返回编辑' }).click();
  await expect(node(page, task.outputNodeId)).toHaveCount(0); await expect(source).toHaveCount(1);
  const projectNodes = async () => {
    const payload = await (await page.request.get(`/api/v1/projects/${id}`)).json();
    return payload.canvases.flatMap((canvas: { nodes: { id: string }[] }) => canvas.nodes).map((item: { id: string }) => item.id);
  };
  expect(await projectNodes()).toEqual(expect.arrayContaining([sourceId, task.outputNodeId]));
  const loadTask = async () => { const payload = await (await page.request.get(`/api/v1/projects/${id}/generations`)).json(); expect(payload.generations).toHaveLength(1); return payload.generations[0]; };
  expect((await loadTask()).input).toEqual(task.input);
  await deleteFromNodeMenu(source); await save(page); await page.reload();
  await expect(page.getByTestId('canvas-node')).toHaveCount(0);
  expect(await projectNodes()).toHaveLength(0);
  const remaining = await loadTask(); expect(remaining.id).toBe(task.id); expect(remaining.input).toEqual(task.input); expect(remaining.status).toBe('awaiting_confirmation'); expect(remaining.reservedCents).toBe(0); expect(remaining.remoteTaskId).toBeNull(); expect(confirms).toBe(0);
});
