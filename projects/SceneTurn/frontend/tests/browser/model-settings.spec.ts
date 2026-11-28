import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

import { expect, test, type Page } from '@playwright/test';
import { addCanvasNode } from './manual-studio';

const runFile = promisify(execFile);

async function createProject(page: Page) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`models-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);

  const created = await page.request.post('/api/v1/projects', { data: { title: '模型设置浏览器验证' } });
  expect(created.status()).toBe(201);
  const { project: { id } } = await created.json();
  return id as string;
}

async function openMaterialModelPicker(page: Page, projectId: string) {
  await page.goto(`/zh-CN/editor?id=${projectId}`);
  await page.getByTitle('素材', { exact: true }).click();
  await page.getByTestId('materials-generate-tab').click();
  await page.getByTestId('material-model-picker').click();
  const picker = page.getByRole('dialog', { name: '新一轮模型' });
  await expect(picker).toBeVisible();
  return picker;
}

test('桌面模型目录、项目默认值与账号隔离', async ({ page, browser }) => {
  await page.setViewportSize({ width: 1280, height: 960 });
  const id = await createProject(page);
  const path = `/zh-CN/settings/models?projectId=${id}`;
  await page.goto(path);
  await expect(page.getByRole('heading', { name: '模型设置' })).toBeVisible();
  const models = page.getByRole('region', { name: '模型目录' });
  await expect(models.getByText('DeepSeek Flash', { exact: true })).toBeVisible();
  await expect(models.getByText('Qwen3.8 Flash', { exact: true })).toBeVisible();
  await expect(models.getByText('商汤 Fast', { exact: true })).toBeVisible();
  await expect(models.getByText('Seedream 5.5', { exact: true })).toBeVisible();
  await expect(models.getByText('个人 Codex', { exact: true })).toBeVisible();
  await expect(models.getByText(/^Seedance/)).toHaveCount(7);
  await expect(models.getByText(/豆包|百炼生图/)).toHaveCount(0);
  await page.getByRole('button', { name: /视频.*Wan 3\.0|Wan 3\.0.*视频/ }).click();
  const video = page.getByRole('dialog', { name: '选择视频模型' });
  await expect(video.getByRole('radio', { name: 'Wan 3.0', exact: true })).toBeChecked();
  await expect(video.getByRole('radio', { name: 'Seedance 2.5', exact: true })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(video).not.toBeVisible();
  await page.getByRole('button', { name: '保存默认模型' }).click();
  await expect(page.getByText('项目默认模型已保存')).toBeVisible();

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.reload();
  await expect(page.getByRole('button', { name: /视频.*Wan 3\.0|Wan 3\.0.*视频/ })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const visitor = await browser.newContext({ baseURL: new URL(page.url()).origin });
  try {
    const registered = await visitor.request.post('/api/v1/auth/register', { data: {
      email: `models-visitor-${crypto.randomUUID()}@example.com`, password: crypto.randomUUID(),
    } });
    expect(registered.status()).toBe(201);
    const forbidden = await visitor.request.get(`/api/v1/settings/model-providers?projectId=${id}`);
    expect(forbidden.status()).toBe(404);
  } finally {
    await visitor.close();
  }

  const picker = await openMaterialModelPicker(page, id);
  await expect(picker.getByText('文本模型', { exact: true })).toBeVisible();
  await expect(picker.getByText('图片模型', { exact: true })).toBeVisible();
  await expect(picker.getByText('视频模型', { exact: true })).toBeVisible();
  await expect(picker.getByRole('radio', { name: '个人 Codex' })).toBeDisabled();
  await expect(picker.getByRole('radio', { name: '商汤 Fast' })).toBeChecked();
  await expect(picker.getByRole('radio', { name: 'Seedream 5.5' })).toBeDisabled();
  await expect(picker.getByRole('radio', { name: 'Seedance 1.0 Pro Fast' })).toHaveCount(0);
  const connectLink = picker.getByRole('link', { name: '去连接个人 Codex' });
  await expect(connectLink).toHaveAttribute('href', new RegExp(`id=${id}.*connectCodex=1`));
  const opened = page.waitForEvent('popup');
  await connectLink.click();
  const connectionPage = await opened;
  await expect(connectionPage.getByTestId('project-codex-chat').getByTestId('chat-connect')).toBeVisible();
  await connectionPage.close();
  await expect(picker).toBeInViewport({ ratio: 1 });
  await picker.getByRole('button', { name: '取消' }).click();
  await expect(picker).not.toBeVisible();
  await expect(page.getByTestId('material-model-picker')).toBeFocused();
  await expect(page.getByRole('button', { name: '个人 Codex 生图', exact: true })).toHaveCount(0);
});

test('在线个人 Codex 可用，取消和本轮选择不修改项目默认值', async ({ page }) => {
  test.skip(process.env.SCENETURN_TEST_CODEX_CONNECTION !== '1', '需要本机已登录且支持原生生图的 Codex');
  test.setTimeout(90000);
  await page.setViewportSize({ width: 1440, height: 960 });
  const id = await createProject(page);
  const pairing = await page.request.post(`/api/v1/projects/${id}/connections/pairings`, { data: {
    operationId: crypto.randomUUID(), tools: ['read_project'], expiresInSeconds: 3600,
  } });
  expect(pairing.status()).toBe(201);
  const { pairingId, pairingCode } = await pairing.json();
  const product = path.resolve(test.info().project.testDir, '../../..');
  const directory = await mkdtemp(path.join(product, '.intermediate', 'model-picker-connection-'));
  const requestFile = path.join(directory, 'pairing.json');
  const cwd = path.join(product, 'scripts/codex-bridge');
  const env = { ...process.env, SCENETURN_BRIDGE_STATE_DIR: directory };
  const bridge = ['--import', 'tsx', 'src/cli.ts'];
  let connectionId: string | undefined;
  try {
    await writeFile(requestFile, JSON.stringify({ apiBase: `${new URL(page.url()).origin}/api/v1`, pairingId, pairingCode }), { mode: 0o600 });
    const paired = await runFile(process.execPath, [...bridge, 'pair', '--request-file', requestFile], { cwd, env, timeout: 15000 });
    connectionId = JSON.parse(paired.stdout).connectionId as string;
    await runFile(process.execPath, [...bridge, 'run', '--once'], { cwd, env, timeout: 30000 });

    const picker = await openMaterialModelPicker(page, id);
    const codex = picker.getByRole('radio', { name: '个人 Codex' });
    const sensetime = picker.getByRole('radio', { name: '商汤 Fast' });
    const codexOption = picker.locator('[data-model-id="codex-image"]');
    await expect(codex).toBeEnabled();
    await expect(sensetime).toBeChecked();
    await codexOption.click();
    await expect(codex).toBeChecked();
    await picker.getByRole('button', { name: '取消' }).click();

    await page.getByTestId('material-model-picker').click();
    await expect(sensetime).toBeChecked();
    await expect(codex).toBeEnabled();
    await codexOption.click();
    await picker.getByRole('button', { name: '应用到下一轮' }).click();
    await expect(picker).not.toBeVisible();
    await expect(page.getByTestId('material-next-models')).toContainText('个人 Codex');

    await page.getByTestId('material-model-picker').click();
    await expect(codex).toBeChecked();
    await page.keyboard.press('Escape');
    await expect(picker).not.toBeVisible();
    const defaults = await page.request.get(`/api/v1/projects/${id}/model-defaults`);
    expect(defaults.ok()).toBe(true);
    expect((await defaults.json()).defaults.image).toBe('sensetime-fast');

    await runFile(process.execPath, [...bridge, 'run', '--once'], { cwd, env, timeout: 30000 });
    await page.goto(`/zh-CN/settings/models?projectId=${id}`);
    await page.getByRole('button', { name: '图片模型 商汤 Fast' }).click();
    const defaultPicker = page.getByRole('dialog', { name: '选择图片模型' });
    await expect(defaultPicker.getByRole('radio', { name: '个人 Codex' })).toBeEnabled();
    await defaultPicker.locator('[data-model-id="codex-image"]').click();
    await defaultPicker.getByRole('button', { name: '选择型号' }).click();
    await expect(page.getByRole('button', { name: '图片模型 个人 Codex' })).toBeVisible();
    await page.getByRole('button', { name: '保存默认模型' }).click();
    await expect(page.getByText('项目默认模型已保存')).toBeVisible();
    const stored = await page.request.get(`/api/v1/projects/${id}/model-defaults`);
    expect(stored.ok()).toBe(true);
    expect((await stored.json()).defaults.image).toBe('codex-image');
    await openMaterialModelPicker(page, id);
    await expect(codex).toBeChecked();

    await page.goto(`/zh-CN/studio?id=${id}`);
    const config = await addCanvasNode(page, 'image');
    const nodeId = await config.getAttribute('data-node-id');
    await config.getByTestId('generation-model-trigger').click();
    await config.getByTestId('generation-model-codex-image').click();
    await config.getByTestId('generation-specifications-trigger').click();
    await config.getByRole('button', { name: '图片比例 9:16', exact: true }).click();
    await config.getByRole('button', { name: '图片尺寸 2K' }).click();
    await config.getByTestId('generation-specifications-trigger').click();
    await config.getByTestId('generation-prompt').fill('空旷车站的高窗，冷白晨光斜照进来，写实电影画面。');
    const prepared = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/canvas-image-jobs'));
    await config.getByTestId('image-generation-prepare').click();
    const response = await prepared;
    expect(response.status()).toBe(201);
    const { job } = await response.json();
    expect(job).toMatchObject({ modelProfileId: 'codex-image', width: 1152, height: 2048, status: 'awaiting_confirmation', assetId: null });
    await page.reload();
    const restored = page.locator(`[data-testid="canvas-node"][data-node-id="${nodeId}"]`);
    await restored.getByTestId('canvas-node-header').click();
    await expect(restored.getByTestId('canvas-image-task')).toContainText('个人 Codex · 1152 × 2048');
    await expect(restored.getByTestId('canvas-image-confirm')).toBeEnabled();
  } finally {
    await rm(directory, { recursive: true, force: true });
    if (connectionId) {
      const revoked = await page.request.post(`/api/v1/projects/${id}/connections/${connectionId}/revoke`, { data: { operationId: crypto.randomUUID() } });
      expect(revoked.ok()).toBe(true);
    }
  }
});
