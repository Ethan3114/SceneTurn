import { expect, test, type Page, type Request } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { resolve } from 'node:path';
import { errors } from 'playwright';
import { createProjectFromList } from './manual-studio';

const materialStatus = /\/(?:material-conversations(?:\/[^/]+)?|media-candidates|generations)$/;
const canvasStatus = /\/(?:generations|text-generations|canvas-image-jobs)$/;
const codexStatus = /\/(?:conversations\/[^/]+|connections)$/;
const transferStatus = /^\/api\/v1\/transfers(?:\/[^/]+)?$/;

function matchesStatus(request: Request, pattern: RegExp) {
  const { pathname } = new URL(request.url());
  return request.method() === 'GET' && pathname.startsWith('/api/v1/') && pattern.test(pathname);
}

async function expectIdle(page: Page, pattern: RegExp) {
  // 观察时间覆盖旧轮询的至少两个周期（最长 2.5 秒）。
  const request = await page.waitForRequest((candidate) => matchesStatus(candidate, pattern), { timeout: 5500 }).catch((error: unknown) => {
    if (error instanceof errors.TimeoutError) return null;
    throw error;
  });
  expect(request?.url(), '空闲时发出了状态请求').toBeUndefined();
}

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`idle-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '空闲请求验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
});

test('空项目的素材岗位与手动画布停止状态轮询', async ({ page }) => {
  await page.getByTitle('素材', { exact: true }).click();
  const initialHistory = page.waitForResponse(async (response) => {
    if (!matchesStatus(response.request(), /\/material-conversations$/) || !response.ok()) return false;
    const { conversations } = await response.json();
    return conversations.length > 0;
  });
  const initialCandidates = page.waitForResponse((response) => matchesStatus(response.request(), /\/media-candidates$/));
  await page.getByTestId('materials-generate-tab').click();
  const workspace = page.getByTestId('material-workspace');
  await expect(workspace.getByTestId('material-role-canvas')).toBeVisible();
  await expect(workspace.getByTestId('material-media-candidates')).toBeVisible();
  for (const response of await Promise.all([initialHistory, initialCandidates])) expect(response.ok()).toBe(true);
  await expectIdle(page, materialStatus);

  await page.getByRole('button', { name: '关闭素材对话' }).click();
  const initialReads = ['generations', 'text-generations', 'canvas-image-jobs'].map((segment) => page.waitForResponse((response) =>
    response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith(`/${segment}`)));
  await workspace.getByRole('button', { name: '手动编辑画布' }).click();
  const canvas = workspace.getByTestId('embedded-material-canvas');
  await expect(canvas.getByTestId('canvas-add-node')).toBeEnabled();
  for (const response of await Promise.all(initialReads)) expect(response.ok()).toBe(true);
  await expectIdle(page, canvasStatus);
});

test('空闲的剧情编排 Codex 对话停止状态轮询', async ({ page }) => {
  const initialReads = [/\/conversations\/[^/]+$/, /\/connections$/].map((pattern) => page.waitForResponse((response) =>
    matchesStatus(response.request(), pattern)));
  await page.getByRole('button', { name: '问问 AI', exact: true }).click();
  const chat = page.getByTestId('project-codex-chat');
  await expect(chat.getByTestId('chat-input')).toBeEnabled();
  for (const response of await Promise.all(initialReads)) expect(response.ok()).toBe(true);
  await expect(chat.getByTestId('chat-connection')).toHaveText('等待本机连接');
  await expectIdle(page, codexStatus);
});

test('项目备份完成后停止状态轮询', async ({ page }) => {
  const initialRead = page.waitForResponse((response) => matchesStatus(response.request(), transferStatus));
  const openTransfer = page.getByTestId('project-export-menu');
  const opensMenu = await openTransfer.getAttribute('aria-haspopup') === 'menu';
  await openTransfer.click();
  if (opensMenu) await page.getByTestId('project-transfer-open').click();
  const dialog = page.getByRole('dialog', { name: '项目备份与游戏导出' });
  await expect(dialog).toBeVisible();
  expect((await initialRead).ok()).toBe(true);
  await expectIdle(page, transferStatus);

  await dialog.getByTestId('project-backup-prepare').click();
  const job = dialog.locator('[data-transfer-id]').filter({ hasText: '完整项目备份' });
  const isolatedData = process.env.OPENFMV_TEST_TRANSFER_DATA;
  if (isolatedData) {
    // 隔离 API 关闭内置工作进程时，观察排队期间的周期请求，再启动同一数据库的真实工作进程。
    await expect(job).toHaveAttribute('data-transfer-status', 'queued');
    const queuedRead = await page.waitForResponse((response) => matchesStatus(response.request(), transferStatus), { timeout: 8000 });
    expect(queuedRead.ok()).toBe(true);
    await expect(job).toHaveAttribute('data-transfer-status', 'queued');
    const backend = resolve('../backend');
    const worker = spawn(resolve(backend, '.venv/bin/python'), [resolve(backend, 'tests/transfer_worker_process.py')], {
      cwd: backend,
      env: { ...process.env, OPENFMV_DATA_DIR: resolve(isolatedData), PYTHONPATH: backend, PYTHONDONTWRITEBYTECODE: '1' },
      stdio: 'inherit',
    });
    await once(worker, 'spawn');
    const stopped = once(worker, 'exit');
    try {
      await expect(job).toHaveAttribute('data-transfer-status', 'ready', { timeout: 10000 });
    } finally {
      worker.kill();
      await stopped;
    }
  }
  await expect(job).toHaveAttribute('data-transfer-status', 'ready', { timeout: 10000 });
  await expect(job.getByTestId('transfer-download')).toBeVisible();
  await expectIdle(page, transferStatus);
});
