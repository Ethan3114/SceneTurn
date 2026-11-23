import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { createProjectFromList, enterManualStudio } from './manual-studio';

const product = path.resolve('..');
const manifest = z.object({ videos: z.array(z.object({ generationId: z.string(), assetId: z.string(), path: z.string(), sha256: z.string().length(64) }).strict()).length(3) }).strict();

test('三段真实首尾帧成片通过受保护媒体接口在画布内播放', async ({ page }) => {
  test.setTimeout(120000);
  const inputs = manifest.parse(JSON.parse(await readFile(path.join(product, '.intermediate/media-sequence-smoke-20261116/browser-media.json'), 'utf8')));
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`generated-playback-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '三段真实Wan视频播放验证');
  await enterManualStudio(page);
  const projectId = new URL(page.url()).searchParams.get('id');
  if (!projectId) throw new Error('真实项目缺少标识');
  const results = [];
  for (const [index, input] of inputs.videos.entries()) {
    const file = path.resolve(product, input.path);
    if (!file.startsWith(path.join(product, 'data/media/'))) throw new Error('视频来源超出本产品媒体目录');
    expect(createHash('sha256').update(await readFile(file)).digest('hex')).toBe(input.sha256);
    const pending = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/v1/projects/${projectId}/assets`);
    await page.getByTestId('canvas-import-file').setInputFiles(file);
    const uploaded = await pending;
    expect(uploaded.status()).toBe(201);
    const asset = (await uploaded.json()).asset;
    expect(asset.sha256).toBe(input.sha256);
    const video = page.locator(`[data-testid="canvas-node"] video[data-canvas-video]`).nth(index);
    await expect(video).toHaveCount(1);
    await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).readyState)).toBeGreaterThanOrEqual(2);
    await video.evaluate(async (element) => { const media = element as HTMLVideoElement; media.muted = true; await media.play(); });
    await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeGreaterThan(0.1);
    const playback = await video.evaluate((element) => {
      const media = element as HTMLVideoElement;
      media.pause();
      return { duration: media.duration, width: media.videoWidth, height: media.videoHeight, currentTime: media.currentTime, error: media.error?.code ?? null };
    });
    expect(playback.duration).toBeCloseTo(asset.durationMs / 1000, 2);
    expect(playback.width).toBe(asset.width);
    expect(playback.height).toBe(asset.height);
    expect(playback.error).toBeNull();
    expect((await page.request.get(`/api/v1/assets/${asset.id}/content`)).status()).toBe(200);
    results.push({ generationId: input.generationId, sourceAssetId: input.assetId, uploadedAssetId: asset.id, sha256: input.sha256, playback });
  }
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  await page.reload();
  await expect(page.locator('video[data-canvas-video]')).toHaveCount(3);
  expect(errors).toEqual([]);
  const directory = path.join(product, 'docs/evidence/阶段2-3');
  await mkdir(directory, { recursive: true });
  await page.screenshot({ path: path.join(directory, '三段真实首尾帧视频播放-1440px.png') });
  await writeFile(path.join(product, '.intermediate/media-sequence-smoke-20261116/browser-playback.json'), JSON.stringify({ verifiedAt: new Date().toISOString(), projectId, reloadVerified: true, results }, null, 2));
});
