import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createProjectFromList } from './manual-studio';

const product = path.resolve('..');

test('完整项目备份与重复导入，前端网页游玩及固定游戏包互动分支', async ({ page, context, browser }, testInfo) => {
  test.setTimeout(240000);
  const captureEvidence = !process.env.OPENFMV_TEST_BASE_URL;
  const directory = captureEvidence ? path.join(product, '.intermediate/project-transfer-smoke-20261116') : testInfo.outputPath('transfers');
  const source = JSON.parse(await readFile(path.join(product, '.intermediate/media-sequence-smoke-20261116/browser-media.json'), 'utf8'));
  await mkdir(directory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`transfers-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '三段真实视频互动导出验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const projectId = new URL(page.url()).searchParams.get('id');
  if (!projectId) throw new Error('项目缺少标识');
  await page.goto('/zh-CN/projects');
  const assetIds: string[] = [];
  for (const [index, item] of source.videos.entries()) {
    const uploaded = await page.request.post(`/api/v1/projects/${projectId}/assets`, { multipart: { file: {
      name: `真实首尾帧视频${index + 1}.mp4`, mimeType: 'video/mp4', buffer: await readFile(path.join(product, item.path)),
    } } });
    expect(uploaded.status()).toBe(201);
    const result = await uploaded.json();
    expect(result.asset.sha256).toBe(item.sha256);
    assetIds.push(result.asset.id);
  }
  let envelope = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
  for (const title of ['第二幕', '第三幕']) {
    const response = await page.request.post(`/api/v1/projects/${projectId}/scenes`, { data: { title, expectedRevision: envelope.revision, operationId: crypto.randomUUID() } });
    expect(response.status()).toBe(201); envelope = await response.json();
  }
  const scenes = envelope.project.graphData.nodes.filter((node: { type: string }) => node.type === 'scene');
  for (const [index, scene] of scenes.entries()) {
    const response = await page.request.post(`/api/v1/projects/${projectId}/timeline-assets`, { data: { sceneId: scene.id, assetId: assetIds[index],
      startTime: 0, duration: 1.5, sourceStart: 0, expectedRevision: envelope.revision, operationId: crypto.randomUUID() } });
    expect(response.status()).toBe(200); envelope = await response.json();
  }
  const graph = structuredClone(envelope.project.graphData);
  const mediaScenes = graph.nodes.filter((node: { type: string }) => node.type === 'scene');
  const start = graph.nodes.find((node: { type: string }) => node.type === 'start');
  const end = graph.nodes.find((node: { type: string }) => node.type === 'end');
  end.data.label = '完成结局';
  const timeout = crypto.randomUUID(), normal = crypto.randomUUID(), qte = crypto.randomUUID();
  graph.nodes.push({ id: timeout, type: 'end', position: { x: 1000, y: 400 }, data: { type: 'end', label: '超时结局' } });
  mediaScenes[0].data.timeline.tracks[1].clips.push({ id: normal, type: 'button', mode: 'normal', label: '进入第二幕', startTime: .15, duration: 1.2,
    enabled: true, pauseOnShow: true, rect: { x: .2, y: .7, width: .6, height: .2 } });
  mediaScenes[0].data.internalPrompt = '这份创作依据保留在工程历史中';
  mediaScenes[1].data.timeline.tracks[1].clips.push({ id: qte, type: 'button', mode: 'qte', label: '及时选择', startTime: .2, duration: 1,
    enabled: true, pauseOnShow: false, rect: { x: .2, y: .7, width: .6, height: .2 }, qte: { input: 'click', clickCount: 1, prompt: '立即选择', showCountdown: true } });
  graph.edges = [
    { id: crypto.randomUUID(), source: start.id, target: mediaScenes[0].id, sourceHandle: 'node:default' },
    { id: crypto.randomUUID(), source: mediaScenes[0].id, target: mediaScenes[1].id, sourceHandle: `button:${normal}:click` },
    { id: crypto.randomUUID(), source: mediaScenes[1].id, target: mediaScenes[2].id, sourceHandle: `button:${qte}:click` },
    { id: crypto.randomUUID(), source: mediaScenes[1].id, target: timeout, sourceHandle: `button:${qte}:timeout` },
    { id: crypto.randomUUID(), source: mediaScenes[2].id, target: end.id, sourceHandle: 'node:default' },
  ];
  const saved = await page.request.put(`/api/v1/projects/${projectId}`, { data: { operationId: crypto.randomUUID(), expectedRevision: envelope.revision,
    project: { ...envelope.project, graphData: graph } } });
  expect(saved.status()).toBe(200);
  envelope = await saved.json();
  await page.goto(`/zh-CN/editor?id=${projectId}`);
  await page.getByTestId('project-export-menu').click();
  await page.getByTestId('project-transfer-open').click();
  const dialog = page.getByRole('dialog', { name: '项目备份与游戏导出' });
  await expect(dialog).toBeVisible();
  await dialog.getByTestId('project-backup-prepare').click();
  const backupCard = dialog.locator('[data-transfer-id]').filter({ hasText: '完整项目备份' }).first();
  await expect(backupCard).toHaveAttribute('data-transfer-status', 'ready');
  const backupId = await backupCard.getAttribute('data-transfer-id');
  const backupDownload = page.waitForEvent('download');
  await backupCard.getByTestId('transfer-download').click();
  const backupFile = path.join(directory, `browser-backup-${crypto.randomUUID()}.zip`);
  await (await backupDownload).saveAs(backupFile);
  await dialog.getByTestId('game-export-prepare').click();
  const gameCard = dialog.locator('[data-transfer-id]').filter({ hasText: '固定游戏版本' }).first();
  await expect(gameCard).toHaveAttribute('data-transfer-status', 'awaiting_confirmation');
  const gameId = await gameCard.getAttribute('data-transfer-id');
  await gameCard.getByTestId('transfer-confirm').click();
  await expect(gameCard).toHaveAttribute('data-transfer-status', 'ready');
  await gameCard.getByText('下载游戏包', { exact: true }).first().click();
  const gameDownload = page.waitForEvent('download');
  await gameCard.getByTestId('transfer-download').click();
  const gameFile = path.join(directory, `browser-game-${crypto.randomUUID()}.zip`);
  await (await gameDownload).saveAs(gameFile);
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await dialog.evaluate((element) => element.getBoundingClientRect().width <= innerWidth)).toBe(true);
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    if (captureEvidence) await page.screenshot({ path: path.join(product, `docs/evidence/阶段2-4/项目备份与固定版本-${width}px.png`) });
  }
  await dialog.getByRole('button', { name: '关闭导出窗口' }).click();
  let onlineDownloads = 0;
  const onlineErrors: string[] = [];
  page.on('download', () => { onlineDownloads += 1; });
  page.on('pageerror', (failure) => onlineErrors.push(failure.message));
  await page.getByTestId('project-play-current').click();
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByRole('button', { name: '进入第二幕', exact: true }).click();
  await page.getByRole('button', { name: '及时选择', exact: true }).click();
  await expect(page.getByRole('button', { name: '重新开始', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Info', exact: true }).click();
  await expect(page.getByRole('heading', { name: '完成结局', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page.getByTestId('project-play-current')).toBeVisible();
  await page.goto('/zh-CN/projects');
  await page.getByTestId('project-item').filter({ hasText: '三段真实视频互动导出验证' }).getByTestId('project-play').click();
  await expect(page).toHaveURL(new RegExp(`/zh-CN/play/${projectId}$`));
  await page.getByRole('button', { name: '继续', exact: true }).click();
  await page.getByRole('button', { name: '进入第二幕', exact: true }).click();
  await expect(page.getByRole('button', { name: '重新开始', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Info', exact: true }).click();
  await expect(page.getByRole('heading', { name: '超时结局', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.goto(`/zh-CN/editor?id=${projectId}`);
  await page.getByTestId('project-export-menu').click();
  await page.getByTestId('project-transfer-open').click();
  await gameCard.getByTestId('transfer-preview').click();
  const fixedUrl = page.url();
  await expect(page).toHaveURL(new RegExp(`/zh-CN/play/${projectId}\\?version=${gameId}`));
  const webFrame = page.frameLocator('[data-testid="web-game-frame"]');
  await webFrame.locator('[data-next]').click();
  await webFrame.locator(`[data-timeline-clip="${normal}"]`).click();
  await webFrame.locator(`[data-timeline-clip="${qte}"]`).click();
  await expect(webFrame.getByRole('heading', { name: '第三幕', exact: true })).toBeVisible();
  await expect.poll(() => webFrame.locator('video').first().evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeGreaterThan(.1);
  await expect(webFrame.getByRole('heading', { name: '完成结局', exact: true })).toBeVisible();
  await page.getByTestId('web-game-restart').click();
  await webFrame.locator('[data-next]').click();
  await webFrame.locator(`[data-timeline-clip="${normal}"]`).click();
  await expect(webFrame.getByRole('heading', { name: '超时结局', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('web-game-frame')).toBeVisible();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.getByTestId('web-game-page').evaluate((element) => element.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.getByTestId('web-game-frame').evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(500);
    expect(await page.getByTestId('web-game-frame').evaluate((element) => element.getBoundingClientRect().bottom <= innerHeight)).toBe(true);
    if (captureEvidence) await page.screenshot({ path: path.join(product, `docs/evidence/阶段2-4/网页游玩-${width}px.png`) });
  }
  const anonymous = await browser.newContext();
  try {
    const denied = await anonymous.newPage();
    await denied.goto(fixedUrl);
    await expect(denied.getByRole('alert')).toBeVisible();
    await expect(denied.getByTestId('web-game-frame')).toHaveCount(0);
    if (!captureEvidence && gameId) {
      await test.step('公开作品搜索、先导片、互动影院和访问撤销', async () => {
        const publicationTitle = `公开影院验证 ${crypto.randomUUID()}`;
        const endpoint = `/api/v1/transfers/${gameId}/publication`;
        const published = await page.request.put(endpoint, { data: {
          published: true, title: publicationTitle, description: '本机公开作品验证', trailerAssetId: assetIds[0],
        } });
        expect(published.status()).toBe(200);
        const { publication } = await published.json();
        await denied.goto('/zh-CN/works');
        await expect(denied.locator('.st-public')).toHaveCSS('background-color', 'rgb(13, 13, 13)');
        await denied.getByRole('searchbox', { name: '搜索作品' }).fill('没有匹配的测试作品');
        await expect(denied.getByText('没有匹配的作品', { exact: true })).toBeVisible();
        await denied.getByRole('searchbox', { name: '搜索作品' }).fill(publicationTitle);
        await denied.getByRole('link', { name: publicationTitle }).click();
        await expect(denied.getByRole('heading', { name: publicationTitle })).toBeVisible();
        await denied.getByRole('link', { name: '观看先导片' }).click();
        await expect(denied.locator('video')).toBeVisible();
        await expect.poll(() => denied.locator('video').evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThan(0);
        await denied.goto(`/zh-CN/works/${publication.slug}`);
        await denied.getByRole('link', { name: '开始游戏' }).click();
        const publicPlayer = denied.frameLocator('iframe');
        await publicPlayer.locator('[data-next]').click();
        await publicPlayer.locator(`[data-timeline-clip="${normal}"]`).click();
        await publicPlayer.locator(`[data-timeline-clip="${qte}"]`).click();
        await expect(publicPlayer.getByRole('heading', { name: '完成结局', exact: true })).toBeVisible();
        expect((await page.request.put(endpoint, { data: { published: false } })).status()).toBe(200);
        await denied.reload();
        await expect(denied.getByText('作品暂不可访问', { exact: true })).toBeVisible();
      });
    }
  } finally { await anonymous.close(); }
  expect(onlineDownloads).toBe(0);
  expect(onlineErrors).toEqual([]);
  await page.getByRole('link', { name: '返回编辑', exact: true }).click();
  await expect(page.getByTestId('project-play-current')).toBeVisible();
  await page.goto('/zh-CN/projects');
  await page.getByTestId('project-import-open').click();
  await page.getByTestId('project-import-file').setInputFiles(backupFile);
  const imported = page.locator('[data-transfer-id]').filter({ hasText: '项目导入' }).first();
  await expect(imported).toHaveAttribute('data-transfer-status', 'awaiting_confirmation');
  const importId = await imported.getAttribute('data-transfer-id');
  await imported.getByTestId('transfer-confirm').click();
  await expect(imported).toHaveAttribute('data-transfer-status', 'ready');
  if (!backupId || !gameId || !importId) throw new Error('真实导入与导出记录缺少标识');
  const importResult = await (await page.request.get(`/api/v1/transfers/${importId}`)).json();
  const importedProjectId = importResult.transfer.projectId;
  const restored = await (await page.request.get(`/api/v1/projects/${importedProjectId}`)).json();
  expect(restored.project.graphData.edges).toEqual(envelope.project.graphData.edges);
  const before = await (await page.request.get('/api/v1/projects')).json();
  const repeated = await page.request.post('/api/v1/transfers/imports', { multipart: { operationId: crypto.randomUUID(),
    file: { name: '再次导入.zip', mimeType: 'application/zip', buffer: await readFile(backupFile) } } });
  expect(repeated.status()).toBe(201);
  expect((await repeated.json()).transfer.id).toBe(importId);
  expect((await (await page.request.get('/api/v1/projects')).json()).projects.length).toBe(before.projects.length);
  const unpacked = path.join(directory, `independent-game-${crypto.randomUUID()}`);
  execFileSync(path.join(product, 'backend/.venv/bin/python'), ['-m', 'zipfile', '-e', gameFile, unpacked], { env: { ...process.env, TMPDIR: path.join(product, '.intermediate/tmp') } });
  const game = JSON.parse(await readFile(path.join(unpacked, 'game.json'), 'utf8'));
  expect(JSON.stringify(game)).not.toContain('这份创作依据保留在工程历史中');
  const offline = await context.newPage();
  const requests: string[] = [], errors: string[] = [];
  offline.on('request', (request) => requests.push(request.url()));
  offline.on('pageerror', (error) => errors.push(error.message));
  await context.setOffline(true);
  const local = pathToFileURL(path.join(unpacked, 'index.html')).href;
  await offline.goto(local);
  await offline.locator('[data-next]').click();
  await offline.locator(`[data-timeline-clip="${normal}"]`).click();
  await offline.locator(`[data-timeline-clip="${qte}"]`).click();
  await expect(offline.getByRole('heading', { name: '第三幕', exact: true })).toBeVisible();
  const video = offline.locator('video').first();
  await expect.poll(() => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeGreaterThan(.1);
  await expect(offline.getByRole('heading', { name: '完成结局', exact: true })).toBeVisible();
  await offline.reload();
  await offline.locator('[data-next]').click();
  await offline.locator(`[data-timeline-clip="${normal}"]`).click();
  await expect(offline.getByRole('heading', { name: '超时结局', exact: true })).toBeVisible();
  expect(requests.every((url) => url.startsWith('file:'))).toBe(true);
  expect(errors).toEqual([]);
  if (captureEvidence) await offline.screenshot({ path: path.join(product, 'docs/evidence/阶段2-4/独立游戏包-QTE超时结局.png') });
  await context.setOffline(false);
  await writeFile(path.join(directory, 'browser-proof.json'), JSON.stringify({ verifiedAt: new Date().toISOString(), projectId, importedProjectId,
    jobIds: [backupId, gameId, importId], offlinePlayback: true, normalButton: true, qteSuccess: true, qteTimeout: true,
    projectImport: true, duplicateImport: true, onlyPackageFiles: true, gameFile, unpacked, requestCount: requests.length,
    editorWebPlay: true, projectWebPlay: true, fixedVersionWebPlay: true, webQteSuccess: true, webQteTimeout: true,
    webReload: true, webDownloadCount: onlineDownloads, anonymousWebAccessDenied: true,
  }, null, 2));
});
