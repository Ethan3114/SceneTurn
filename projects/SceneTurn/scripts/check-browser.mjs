import assert from 'node:assert/strict';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, chmod, readFile, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const { values: options } = parseArgs({ options: { 'generated-task-id': { type: 'string' }, 'data-dir': { type: 'string', default: 'data' } } });

const productRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = path.join(productRoot, '.intermediate', 'tmp');
const browserDirectory = path.join(productRoot, '.intermediate', 'playwright');
const runDirectory = path.join(productRoot, '.intermediate', 'browser', randomUUID());
const evidenceDirectory = path.join(productRoot, 'docs', 'evidence', '阶段2-1');
process.env.TMPDIR = temporary;
process.env.XDG_CACHE_HOME = path.join(productRoot, '.intermediate', 'cache');
process.env.PLAYWRIGHT_BROWSERS_PATH = browserDirectory;
await Promise.all([temporary, browserDirectory, runDirectory, evidenceDirectory].map((directory) => mkdir(directory, { recursive: true })));

const require = createRequire(path.join(productRoot, 'frontend', 'package.json'));
const { chromium, expect: baseExpect } = require('@playwright/test');
const expect = baseExpect.configure({ timeout: 30000 });
const baseUrl = process.env.SCENETURN_WEB_URL ?? 'http://127.0.0.1:3210';
const apiUrl = process.env.SCENETURN_API_URL ?? 'http://127.0.0.1:8310/api/v1';
const email = `browser-${randomUUID()}@example.com`;
const password = randomBytes(12).toString('base64url');
const marker = `浏览器保存验证-${randomUUID().slice(0, 8)}`;
const sceneTitle = `实际剧情-${randomUUID().slice(0, 8)}`;
const pageErrors = [];
const browser = await chromium.launch({ headless: true });

async function generatedMediaSource(taskId) {
  const { DatabaseSync } = await import('node:sqlite');
  const directory = await realpath(path.resolve(productRoot, options['data-dir']));
  assert(!path.relative(productRoot, directory).startsWith('..'), '真实素材数据目录必须位于本产品内');
  const currentDatabase = path.join(directory, 'sceneturn.sqlite3');
  const legacyDatabase = path.join(directory, 'openfmv.sqlite3');
  assert(!(existsSync(currentDatabase) && existsSync(legacyDatabase)), '数据目录包含两个数据库，无法确认应读取的文件');
  const databaseFile = await realpath(existsSync(legacyDatabase) ? legacyDatabase : currentDatabase);
  assert(!path.relative(directory, databaseFile).startsWith('..'), '数据库必须位于指定数据目录内');
  const database = new DatabaseSync(databaseFile, { readOnly: true });
  try {
    const source = database.prepare(`
      SELECT g.id AS generationId, g.status, g.remote_task_id AS remoteTaskId,
             g.asset_id AS assetId, g.project_id AS projectId, g.owner_id AS ownerId,
             a.relpath, a.bytes, a.sha256, a.mime_type AS mimeType,
             a.generation_id AS assetGenerationId, a.project_id AS assetProjectId,
             a.owner_id AS assetOwnerId, p.owner_id AS projectOwnerId,
             p.revision, p.data_json AS projectData
      FROM generations g JOIN assets a ON a.id = g.asset_id JOIN projects p ON p.id = g.project_id
      WHERE g.id = ?
    `).get(taskId);
    assert(source, '必须使用已经保存真实视频的任务');
    assert.equal(source.status, 'ready');
    assert(source.remoteTaskId, '真实任务必须具有供应商任务标识');
    assert.equal(source.assetGenerationId, source.generationId);
    assert.equal(source.assetProjectId, source.projectId);
    assert.equal(source.assetOwnerId, source.ownerId);
    assert.equal(source.projectOwnerId, source.ownerId);
    assert.equal(source.mimeType, 'video/mp4');
    const mediaRoot = await realpath(path.join(directory, 'media'));
    const mediaFile = await realpath(path.join(directory, source.relpath));
    assert(!path.relative(mediaRoot, mediaFile).startsWith('..'), '真实视频必须位于指定数据目录的媒体目录内');
    const bytes = await readFile(mediaFile);
    assert.equal(bytes.length, source.bytes);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), source.sha256);
    return {
      generationId: source.generationId,
      remoteTaskId: source.remoteTaskId,
      assetId: source.assetId,
      projectId: source.projectId,
      mediaFile,
      bytes,
      sha256: source.sha256,
      projectRevision: source.revision,
      projectSha256: createHash('sha256').update(source.projectData).digest('hex'),
    };
  } finally {
    database.close();
  }
}

async function verifyGeneratedPlayback(context, page, projectId, taskId) {
  const source = await generatedMediaSource(taskId);
  assert.notEqual(projectId, source.projectId, '真实视频播放检查必须使用独立测试项目');
  await page.goto(`${baseUrl}/zh-CN/studio?id=${encodeURIComponent(projectId)}`, { waitUntil: 'networkidle' });
  const fileInput = page.getByTestId('canvas-import-file');
  await expect(fileInput).toBeAttached();
  const uploadUrl = `${baseUrl}/api/v1/projects/${projectId}/assets`;
  const started = performance.now();
  const uploadResponse = page.waitForResponse((response) => response.url() === uploadUrl && response.request().method() === 'POST');
  await fileInput.setInputFiles(source.mediaFile);
  const uploaded = await uploadResponse;
  const upload = { status: uploaded.status(), body: await uploaded.json(), durationMilliseconds: performance.now() - started };
  assert([200, 201].includes(upload.status), JSON.stringify(upload.body));
  console.log(`真实视频正式上传：${new URL(uploadUrl).origin}，HTTP ${upload.status}，${upload.durationMilliseconds.toFixed(2)}ms，${source.bytes.length}字节。`);
  const asset = upload.body.asset;
  assert.equal(asset.sha256, source.sha256, '正式上传必须保存同一真实生成视频');
  assert.equal(asset.bytes, source.bytes.length);
  const mediaUrl = new URL(asset.url, baseUrl).href;
  assert.equal(new URL(mediaUrl).origin, new URL(baseUrl).origin);
  const videoNode = page.getByTestId('canvas-node').filter({ has: page.locator('video') }).last();
  await expect(videoNode.locator('video')).toBeVisible();
  const playbackNodeId = await videoNode.getAttribute('data-node-id');
  assert(playbackNodeId, '真实上传视频必须进入实际画布节点');
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const range = await page.evaluate(async (url) => {
    const response = await fetch(url, { headers: { Range: 'bytes=0-15' }, signal: AbortSignal.timeout(30000) });
    return { status: response.status, bytes: [...new Uint8Array(await response.arrayBuffer())] };
  }, mediaUrl);
  assert.equal(range.status, 206);
  assert.deepEqual(Buffer.from(range.bytes), source.bytes.subarray(0, 16));
  const anonymous = await browser.newContext();
  try {
    assert.equal((await anonymous.request.get(mediaUrl)).status(), 401, '真实视频URL必须由本人会话保护');
  } finally {
    await anonymous.close();
  }
  const playback = await page.evaluate(async ({ nodeId, expectedUrl }) => {
    const video = document.querySelector(`[data-testid="canvas-node"][data-node-id="${CSS.escape(nodeId)}"] video`);
    if (!(video instanceof HTMLVideoElement)) throw new Error('真实视频节点播放器未显示');
    if (video.src !== expectedUrl) throw new Error('真实节点播放器没有使用正式同源素材URL');
    video.muted = true;
    const events = [];
    for (const name of ['loadedmetadata', 'loadeddata', 'play', 'playing', 'timeupdate', 'seeking', 'seeked', 'error']) {
      video.addEventListener(name, () => events.push({ name, currentTime: video.currentTime, atMilliseconds: performance.now() }));
    }
    const waitFor = (event, predicate = () => true) => new Promise((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timeout);
        video.removeEventListener(event, completed);
        video.removeEventListener('error', failed);
      };
      const completed = () => {
        if (predicate()) {
          cleanup();
          resolve();
        }
      };
      const failed = () => {
        cleanup();
        reject(new Error(`实际视频播放失败：${video.error?.code} ${video.error?.message}`));
      };
      const timeout = setTimeout(() => {
        cleanup();
        reject(new Error(`实际视频未完成${event}事件`));
      }, 30000);
      video.addEventListener(event, completed);
      video.addEventListener('error', failed, { once: true });
    });
    const metadata = waitFor('loadedmetadata');
    video.load();
    await metadata;
    const durationSeconds = video.duration;
    const width = video.videoWidth;
    const height = video.videoHeight;
    const advancing = waitFor('timeupdate', () => video.currentTime >= Math.min(0.5, durationSeconds * 0.2));
    await video.play();
    await advancing;
    video.pause();
    const playedToSeconds = video.currentTime;
    const seekTargetSeconds = durationSeconds * 0.7;
    const seeked = waitFor('seeked');
    video.currentTime = seekTargetSeconds;
    await seeked;
    const seekActualSeconds = video.currentTime;
    const resumed = waitFor('timeupdate', () => video.currentTime >= seekActualSeconds + Math.min(0.2, durationSeconds * 0.1));
    await video.play();
    await resumed;
    video.pause();
    const result = { durationSeconds, width, height, playedToSeconds, seekTargetSeconds, seekActualSeconds, resumedToSeconds: video.currentTime, mediaError: video.error, events };
    return result;
  }, { nodeId: playbackNodeId, expectedUrl: mediaUrl });
  assert(Number.isFinite(playback.durationSeconds) && playback.durationSeconds > 0);
  assert(playback.width > 0 && playback.height > 0);
  assert(playback.playedToSeconds > 0);
  assert(Math.abs(playback.seekActualSeconds - playback.seekTargetSeconds) < 0.25);
  assert(playback.resumedToSeconds > playback.seekActualSeconds);
  assert.equal(playback.mediaError, null);
  for (const required of ['loadedmetadata', 'play', 'timeupdate', 'seeking', 'seeked']) assert(playback.events.some((event) => event.name === required), `缺少实际${required}事件`);
  const after = await generatedMediaSource(taskId);
  assert.equal(after.projectRevision, source.projectRevision, '验证期间原用户项目版本必须保持一致');
  assert.equal(after.projectSha256, source.projectSha256, '验证期间原用户项目内容必须保持一致');
  const result = {
    verifiedAt: new Date().toISOString(), browser: browser.version(), generationId: taskId, remoteTaskId: source.remoteTaskId,
    sourceAssetId: source.assetId, uploadedAssetId: asset.id, testProjectId: projectId, playbackNodeId,
    mediaUrl, bytes: source.bytes.length, sha256: source.sha256,
    uploadOrigin: new URL(uploadUrl).origin, uploadStatus: upload.status, uploadDurationMilliseconds: upload.durationMilliseconds,
    sameOriginUpload: new URL(uploadUrl).origin === new URL(baseUrl).origin,
    rangeStatus: range.status, anonymousStatus: 401, originalProjectUnchanged: true, ...playback,
  };
  const resultFile = path.join(runDirectory, 'generated-playback.json');
  await writeFile(resultFile, JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(`真实生成视频Chromium播放通过：${playback.durationSeconds.toFixed(3)}s，${playback.width}×${playback.height}；播放时间推进、seek及跳转后播放通过，无media.error。`);
  console.log(`真实播放结果：${path.relative(productRoot, resultFile)}`);
}

async function measureEditingResponse(page, nodeId, initialContent) {
  const editor = page.locator(`[data-testid="canvas-node"][data-node-id="${nodeId}"]`).getByTestId('canvas-text');
  await editor.focus();
  await editor.press('ControlOrMeta+A');
  await editor.press('ArrowRight');
  const durations = [];
  const measurementStartedAt = new Date().toISOString();
  let content = initialContent;
  for (let index = 0; index < 30; index += 1) {
    const character = String.fromCharCode(97 + (index % 26));
    content += character;
    await page.evaluate(({ targetNodeId, expectedContent }) => {
      const input = document.querySelector(`[data-testid="canvas-node"][data-node-id="${CSS.escape(targetNodeId)}"] [data-testid="canvas-text"]`);
      if (!(input instanceof HTMLTextAreaElement)) throw new Error('实际画布文本输入框不可用');
      window.__sceneturnResponseProbe = new Promise((resolve, reject) => {
        let timeout;
        const start = () => {
          const started = performance.now();
          requestAnimationFrame(() => requestAnimationFrame(() => {
            clearTimeout(timeout);
            if (input.value !== expectedContent) reject(new Error('实际键盘输入后节点内文字没有正确展示'));
            else resolve(performance.now() - started);
          }));
        };
        input.addEventListener('input', start, { capture: true, once: true });
        timeout = setTimeout(() => {
          input.removeEventListener('input', start, true);
          reject(new Error('节点内编辑没有产生实际input事件'));
        }, 5000);
      });
    }, { targetNodeId: nodeId, expectedContent: content });
    await editor.pressSequentially(character);
    const elapsed = await page.evaluate(async () => {
      const result = await window.__sceneturnResponseProbe;
      delete window.__sceneturnResponseProbe;
      return result;
    });
    assert(Number.isFinite(elapsed) && elapsed >= 0, '本地响应测量必须来自实际输入与展示更新');
    durations.push(elapsed);
  }
  await expect(editor).toHaveValue(content);
  const ordered = [...durations].sort((left, right) => left - right);
  const result = {
    measuredAt: new Date().toISOString(),
    measurementStartedAt,
    method: '真实键盘input事件至节点内编辑文字正确展示后的双动画帧，另经正式保存接口核对内容',
    browser: browser.version(),
    viewport: page.viewportSize(),
    samples: durations.length,
    p95Milliseconds: ordered[Math.ceil(ordered.length * 0.95) - 1],
    maximumMilliseconds: ordered.at(-1),
    durationsMilliseconds: durations,
    finalContent: content,
  };
  await writeFile(path.join(runDirectory, 'editing-performance.json'), JSON.stringify(result, null, 2), { mode: 0o600 });
  assert(result.p95Milliseconds <= 200, `本地编辑P95为${result.p95Milliseconds.toFixed(2)}ms，超过200ms`);
  console.log(`本地编辑响应：${result.samples}次，P95 ${result.p95Milliseconds.toFixed(2)}ms，最大${result.maximumMilliseconds.toFixed(2)}ms，1440px Chromium。`);
  return result;
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', reducedMotion: 'reduce' });
  const page = await context.newPage();
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto(`${baseUrl}/zh-CN/account`, { waitUntil: 'networkidle' });
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(email);
  await page.getByTestId('auth-password').fill(password);
  const registrationResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/auth/register') && response.request().method() === 'POST');
  await page.getByTestId('auth-submit').click();
  const registration = await registrationResponse;
  assert.equal(registration.status(), 201, await registration.text());
  await expect(page.getByTestId('project-title')).toBeVisible();
  await expect(page.getByTestId('payment-status')).toContainText('未配置');
  await page.getByTestId('project-title').fill(marker);
  await page.getByTestId('project-create').click();
  await expect(page.getByTestId('canvas-add-text')).toBeVisible();
  const projectId = new URL(page.url()).searchParams.get('id');
  assert(projectId, '工作台URL必须携带真实项目ID');
  await page.getByTestId('scene-title').fill(sceneTitle);
  await page.getByTestId('scene-create').click();
  await expect(page.getByText(sceneTitle, { exact: true }).first()).toBeVisible();
  await page.getByTestId('canvas-add-text').click();
  const createdNode = page.getByTestId('canvas-node').last();
  await createdNode.getByTestId('canvas-node-body').dblclick();
  await createdNode.getByTestId('canvas-text').fill(marker);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');

  const stored = await context.request.get(`${apiUrl}/projects/${projectId}`);
  assert.equal(stored.status(), 200);
  const envelope = await stored.json();
  assert(envelope.canvases.some((canvas) => canvas.nodes.some((node) => node.metadata?.content === marker)), '真实服务必须保存浏览器写入的画布内容');
  await page.reload({ waitUntil: 'networkidle' });
  const savedNode = page.getByTestId('canvas-node').filter({ hasText: marker });
  await expect(savedNode).toHaveCount(1);
  await savedNode.getByTestId('canvas-node-body').dblclick();
  await expect(savedNode.getByTestId('canvas-text')).toHaveValue(marker);
  const targetNodeId = await savedNode.getAttribute('data-node-id');
  assert(targetNodeId, '实际画布节点必须具有稳定标识');
  const performanceResult = await measureEditingResponse(page, targetNodeId, marker);
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toContainText('已保存');
  const measuredSaved = await context.request.get(`${apiUrl}/projects/${projectId}`);
  assert.equal(measuredSaved.status(), 200);
  const measuredEnvelope = await measuredSaved.json();
  assert(measuredEnvelope.canvases.some((canvas) => canvas.nodes.some((node) => node.id === targetNodeId && node.metadata?.content === performanceResult.finalContent)), '节点内实际键盘编辑必须完整持久保存');

  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(page.getByTestId('canvas-add-text')).toBeVisible();
    const layout = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: window.innerWidth }));
    assert(layout.content <= layout.viewport + 2, `${width}px宽度存在页面水平溢出：${layout.content}`);
    await page.screenshot({ path: path.join(evidenceDirectory, `剧情画布-${width}px.png`), fullPage: true });
  }

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseUrl}/zh-CN/settings/wan`, { waitUntil: 'networkidle' });
  await expect(page.getByTestId('wan-workspace')).toBeVisible();
  await expect(page.getByTestId('wan-key')).toHaveAttribute('type', 'password');
  await expect(page.getByText('尚未配置服务', { exact: true })).toBeVisible();
  await page.screenshot({ path: path.join(evidenceDirectory, '生成配置-未配置.png'), fullPage: true });
  await page.goto(`${baseUrl}/zh-CN/settings/text`, { waitUntil: 'networkidle' });
  await expect(page.getByTestId('text-price-table')).toBeVisible();
  await expect(page.getByTestId('text-billing-status')).toContainText('尚未配置');
  await expect(page.getByTestId('text-api-key')).toHaveCount(0);
  await expect(page.getByTestId('text-admin-only')).toBeVisible();

  if (options['generated-task-id']) await verifyGeneratedPlayback(context, page, projectId, options['generated-task-id']);

  const cookieFile = path.join(runDirectory, 'session.json');
  await context.storageState({ path: cookieFile });
  await chmod(cookieFile, 0o600);
  await page.getByTestId('logout').click();
  await expect(page.getByTestId('auth-submit')).toBeVisible();
  const revoked = await context.request.get(`${apiUrl}/auth/me`);
  assert.equal(revoked.status(), 401, '退出登录后真实会话必须失效');
  assert.deepEqual(pageErrors, [], '浏览器不应出现未处理的运行异常');
  console.log('真实浏览器验证通过：注册、项目、场景、画布保存与恢复、配置状态、支付状态、三种宽度及退出登录。');
  console.log(`截图目录：${path.relative(productRoot, evidenceDirectory)}`);
  await context.close();
} finally {
  await browser.close();
}
