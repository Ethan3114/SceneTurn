import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { createProjectFromList } from './manual-studio';

const product = path.resolve('..');

async function project(page: Page) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`candidates-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '连续首尾帧与候选验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('项目地址缺少标识');
  const value = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const scene = value.project.graphData.nodes.find((node: { type: string }) => node.type === 'scene');
  return { id, sceneId: scene.id as string };
}

async function upload(page: Page, id: string, file: string, mimeType: string, name: string) {
  const response = await page.request.post(`/api/v1/projects/${id}/assets`, { multipart: { file: { name, mimeType, buffer: await readFile(file) } } });
  expect(response.status()).toBe(201);
  return response.json();
}

async function images(page: Page, id: string, count: number) {
  const proof = JSON.parse(await readFile(path.join(product, '.intermediate/sensetime-queue-20261115/proof.json'), 'utf8'));
  const records = proof.records.filter((record: { status: string }) => record.status === 'ready').slice(0, count);
  const ids: string[] = [];
  for (const [index, record] of records.entries()) {
    const result = await upload(page, id, path.join(product, `data/media/images/${record.jobId}.png`), 'image/png', `边界图${index + 1}.png`);
    ids.push(result.asset.id);
  }
  return ids;
}

async function openCandidates(page: Page) {
  await page.reload();
  await page.getByTitle('素材', { exact: true }).click();
  await page.getByTestId('materials-generate-tab').click();
  const close = page.getByRole('button', { name: '关闭素材对话' });
  if (await close.isVisible()) await close.click();
  const area = page.getByTestId('material-media-candidates');
  await area.scrollIntoViewIfNeeded();
  await expect(area.getByText('连续视频与候选', { exact: true })).toBeVisible();
  return area;
}

test('实际图片通过界面形成三段共享首尾帧，重复准备保留独立候选', async ({ page }) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { id, sceneId } = await project(page);
  const frameIds = await images(page, id, 4);
  const area = await openCandidates(page);
  await area.getByText('手动选择连续首尾图片', { exact: true }).click();
  await area.getByRole('button', { name: '添加边界图片' }).click();
  await area.getByRole('button', { name: '添加边界图片' }).click();
  for (let index = 0; index < 4; index += 1) await area.getByRole('combobox', { name: `边界图片${index + 1}`, exact: true }).selectOption(frameIds[index]);
  for (let index = 0; index < 3; index += 1) await area.getByRole('textbox', { name: `片段${index + 1}运动要求` }).fill('固定机位，纸艺平滑折叠，保持木桌与侧光稳定。');
  await area.getByRole('button', { name: '保存连续视频序列' }).click();
  await expect(area.locator('.material-sequence')).toHaveCount(1);
  await expect(area.locator('.material-segment')).toHaveCount(3);
  for (let index = 0; index < 3; index += 1) {
    const segment = area.locator('.material-segment').nth(index);
    expect(await segment.locator('img').first().getAttribute('src')).toContain(frameIds[index]);
    expect(await segment.locator('img').last().getAttribute('src')).toContain(frameIds[index + 1]);
    await segment.getByRole('button', { name: '准备本段视频' }).click();
    await expect(area.locator('[data-candidate-id]')).toHaveCount(index + 1);
  }
  await area.locator('.material-segment').first().getByRole('button', { name: '准备本段视频' }).click();
  await expect(area.locator('[data-candidate-id]')).toHaveCount(4);
  await expect(area.getByRole('button', { name: '确认生成本段视频' })).toHaveCount(4);
  const response = await (await page.request.get(`/api/v1/projects/${id}/scenes/${sceneId}/media-candidates`)).json();
  expect(response.candidates.every((item: { status: string }) => item.status === 'awaiting_confirmation')).toBe(true);
  const requests = await (await page.request.get(`/api/v1/projects/${id}/generations`)).json();
  expect(requests.generations.every((item: { remoteTaskId: string | null }) => item.remoteTaskId === null)).toBe(true);
  await expect(area.locator('pre')).toHaveCount(0);
  await expect(area).not.toContainText('¥');
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await area.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const directory = path.join(product, 'docs/evidence/阶段2-3');
    await mkdir(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, `连续首尾帧与候选-${width}px.png`) });
  }
  await openCandidates(page);
  await expect(page.getByTestId('material-media-candidates').locator('[data-candidate-id]')).toHaveCount(4);
  expect(errors).toEqual([]);
});

test('实际视频候选确认后局部采用，短素材冲突刷新后仍可保留当前版本', async ({ page }) => {
  test.setTimeout(120000);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { id, sceneId } = await project(page);
  const frameIds = await images(page, id, 2);
  const source = path.join(product, '.intermediate/test-media/flower.mp4');
  if (!existsSync(source)) throw new Error('需要已验证的真实CC0视频文件');
  const first = await upload(page, id, source, 'video/mp4', '原视频.mp4');
  const added = await page.request.post(`/api/v1/projects/${id}/timeline-assets`, { data: { sceneId, assetId: first.asset.id, startTime: 1.2, duration: 1.4, sourceStart: .3, expectedRevision: first.revision, operationId: crypto.randomUUID() } });
  expect(added.status()).toBe(200);
  const initial = await added.json();
  const clip = initial.project.graphData.nodes.find((node: { id: string }) => node.id === sceneId).data.timeline.tracks.flatMap((track: { clips: { id: string; assetId: string }[] }) => track.clips).find((item: { assetId: string }) => item.assetId === first.asset.id);
  const candidate = await upload(page, id, source, 'video/mp4', '新候选.mp4');
  const area = await openCandidates(page);
  const conversations = await (await page.request.get(`/api/v1/projects/${id}/scenes/${sceneId}/material-conversations`)).json();
  const latest = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const created = await page.request.post(`/api/v1/projects/${id}/scenes/${sceneId}/media-sequences`, { data: { title: '指定片段候选', conversationId: conversations.conversations[0].id, frameAssetIds: frameIds,
    segments: [{ title: '目标片段', prompt: '保持画面稳定。', duration: 2, resolution: '720P', clipId: clip.id }], expectedRevision: latest.revision, operationId: crypto.randomUUID() } });
  expect(created.status(), await created.text()).toBe(201);
  const sequence = await created.json();
  const checked = await page.request.post(`/api/v1/projects/${id}/media-adoptions/check`, { data: { sceneId, assetId: candidate.asset.id, clipId: clip.id, mode: 'replace', expectedRevision: sequence.project.revision, operationId: crypto.randomUUID() } });
  expect(checked.status()).toBe(201);
  await expect(area.getByRole('button', { name: '确认采用，保留现有编辑' })).toBeVisible();
  await area.getByRole('button', { name: '确认采用，保留现有编辑' }).click();
  await expect(area.getByRole('button', { name: '已采用到目标片段' })).toBeDisabled();
  const after = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  const savedClip = after.project.graphData.nodes.find((node: { id: string }) => node.id === sceneId).data.timeline.tracks.flatMap((track: { clips: { id: string }[] }) => track.clips).find((item: { id: string }) => item.id === clip.id);
  expect(savedClip.assetId).toBe(candidate.asset.id);
  expect(savedClip.startTime).toBe(clip.startTime); expect(savedClip.sourceStart).toBe(clip.sourceStart); expect(savedClip.duration).toBe(clip.duration);
  const short = path.join(product, `.intermediate/test-media/candidate-${crypto.randomUUID()}.mp4`);
  execFileSync('ffmpeg', ['-v', 'error', '-i', source, '-t', '0.4', '-c:v', 'libx264', '-an', short], { env: { ...process.env, TMPDIR: path.join(product, '.intermediate/tmp') } });
  const shorter = await upload(page, id, short, 'video/mp4', '较短候选.mp4');
  const conflict = await page.request.post(`/api/v1/projects/${id}/media-adoptions/check`, { data: { sceneId, assetId: shorter.asset.id, clipId: clip.id, mode: 'replace', expectedRevision: shorter.revision, operationId: crypto.randomUUID() } });
  expect(conflict.status()).toBe(201);
  const restored = await openCandidates(page);
  await expect(restored.locator('[data-adoption-status="duration_conflict"]')).toBeVisible();
  await restored.locator('[data-adoption-status="duration_conflict"]').getByRole('button', { name: '保留当前版本' }).click();
  await expect(restored.locator('[data-adoption-status="duration_conflict"]')).toHaveCount(0);
  const final = await (await page.request.get(`/api/v1/projects/${id}`)).json();
  expect(final.project.graphData).toEqual(after.project.graphData);
});
