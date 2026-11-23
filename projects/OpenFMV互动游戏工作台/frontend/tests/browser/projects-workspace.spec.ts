import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { createProjectFromList } from './manual-studio';

test('项目工作台遵循桌面稿的空状态、建立项目、封面、账户及导入流程', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  const email = `project-workspace-${crypto.randomUUID()}@example.com`;
  await page.getByTestId('auth-email').fill(email);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole('heading', { name: '还没有项目' })).toBeVisible();

  const header = page.getByRole('navigation', { name: '工作台导航' });
  const rail = page.getByRole('navigation', { name: '项目快捷入口' });
  await expect(rail.getByRole('link', { name: '我的项目' })).toHaveAttribute('aria-current', 'page');
  await expect(rail.getByRole('link', { name: '素材库' })).toHaveAttribute('href', '/zh-CN/assets');
  await expect(rail.getByRole('link', { name: '作品馆' })).toHaveAttribute('href', '/zh-CN/works');

  const accountButton = header.getByRole('button', { name: '账户', exact: true });
  await accountButton.click();
  const accountMenu = page.getByRole('region', { name: '账户', exact: true });
  await expect(accountMenu).toContainText(email);
  await expect(accountMenu.getByRole('link', { name: '模型设置' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(accountMenu).toBeHidden();

  const createButton = page.getByTestId('project-new');
  await createButton.click();
  const dialog = page.getByRole('dialog', { name: '新建项目' });
  await expect(dialog.getByTestId('project-title')).toBeFocused();
  await dialog.getByRole('button', { name: '取消' }).click();
  await expect(createButton).toBeFocused();
  const title = '项目设计封面验证';
  await createProjectFromList(page, title);
  await expect(page).toHaveURL(/\/editor\?id=/);
  const projectId = new URL(page.url()).searchParams.get('id');
  if (!projectId || !testInfo.config.configFile) throw new Error('缺少验证项目标识或浏览器配置文件');

  await page.goto('/zh-CN/projects');
  const recent = page.getByTestId('project-feature');
  await expect(recent.locator('[data-has-poster]')).toHaveAttribute('data-has-poster', 'false');
  const src = path.join(path.dirname(testInfo.config.configFile), 'public/sceneturn/tide.jpg');
  const uploaded = await page.request.post(`/api/v1/projects/${projectId}/assets`, { multipart: {
    file: { name: '项目封面.jpg', mimeType: 'image/jpeg', buffer: await readFile(src) },
  } });
  expect(uploaded.status()).toBe(201);
  const { asset } = await uploaded.json() as { asset: { id: string } };
  await page.reload();
  await expect.poll(() => recent.locator('img').evaluate((image) => new URL((image as HTMLImageElement).src).pathname))
    .toBe(`/api/v1/assets/${asset.id}/content`);
  await expect.poll(() => recent.locator('img').evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  const item = page.getByTestId('project-item').filter({ hasText: title });
  await expect(item.getByTestId('project-play')).toHaveAttribute('href', `/zh-CN/play/${projectId}`);

  for (const width of [1440, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    expect((await rail.boundingBox())?.width).toBe(220);
    expect((await recent.boundingBox())?.height).toBe(326);
    expect(await page.locator('.fmv-project-library').evaluate((library) => library.scrollWidth <= innerWidth)).toBe(true);
  }

  await page.getByTestId('project-import-open').click();
  await expect(page).toHaveURL(/\/projects\?view=import$/);
  await expect(page.getByRole('heading', { name: '导入与恢复完整项目' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '导入前会检查' })).toBeVisible();
  await expect(page.getByTestId('project-import-file')).toHaveAttribute('accept', '.zip,application/zip');
  await page.getByRole('link', { name: '返回项目' }).click();
  await expect(item).toBeVisible();
});
