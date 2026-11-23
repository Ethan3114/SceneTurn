import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 1440, height: 900 } });

test('首页试玩由访客开启，两条选择分别抵达对应结局', async ({ page }) => {
  await page.goto('/zh-CN');

  const trial = page.locator('#trial');
  const start = trial.getByRole('button', { name: '开始试玩' });
  await expect(start).toBeVisible();
  await expect(trial.getByRole('button', { name: '点亮信标' })).toHaveCount(0);

  await start.press('Enter');
  const decision = trial.getByRole('heading', { name: '最后一份电力', exact: true });
  await expect(decision).toBeFocused();
  await expect(trial.locator('[data-phase="choice"]')).toHaveCSS('animation-duration', '0.2s');

  await trial.getByRole('button', { name: '点亮信标' }).press('Enter');
  await expect(trial.getByRole('heading', { name: '灯火重燃', exact: true })).toBeFocused();
  await expect(trial.locator('img')).toHaveAttribute('src', '/sceneturn/signal.jpg');
  await expect.poll(() => trial.locator('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);

  await trial.getByRole('button', { name: '重新选择' }).click();
  await expect(decision).toBeFocused();
  await trial.getByRole('button', { name: '关闭闸门' }).click();
  await expect(trial.getByRole('heading', { name: '闸门关上', exact: true })).toBeFocused();
  await expect(trial.locator('img')).toHaveAttribute('src', '/sceneturn/port.jpg');
  await expect.poll(() => trial.locator('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);

  await trial.getByRole('button', { name: '退出试玩' }).click();
  await expect(trial.getByRole('heading', { name: '潮汐尽头', exact: true })).toBeFocused();
  await expect(start).toBeVisible();
});

test('开启减少动态效果时直接切换试玩画面', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/zh-CN');

  const trial = page.locator('#trial');
  await trial.getByRole('button', { name: '开始试玩' }).click();
  await expect(trial.locator('[data-phase="choice"]')).toHaveCSS('animation-name', 'none');
  await trial.getByRole('button', { name: '关闭闸门' }).click();
  await expect(trial.locator('[data-phase="gate"]')).toHaveCSS('animation-name', 'none');
  await expect(trial.getByRole('heading', { name: '闸门关上', exact: true })).toBeVisible();
});
