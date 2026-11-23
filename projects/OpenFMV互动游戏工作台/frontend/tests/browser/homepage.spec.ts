import { expect, test } from '@playwright/test';

test('桌面首页使用已确认的导航、剧照与创作入口', async ({ page }) => {
  for (const width of [1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/zh-CN');
    await expect(page.getByRole('heading', { level: 1 })).toContainText(/让观众决定\s*下一幕/);
    const header = page.getByRole('banner');
    const studio = header.getByRole('link', { name: '进入创作台' });
    const menu = header.getByRole('button', { name: '菜单' });
    await expect(studio).toHaveAttribute('href', '/zh-CN/projects');
    await expect(studio).toHaveCSS('background-color', 'rgb(41, 41, 41)');
    await expect(menu).toHaveCSS('background-color', 'rgb(41, 41, 41)');
    await expect(header.getByRole('link', { name: '试玩', exact: true })).toHaveCount(0);
    await expect(header.getByRole('link', { name: '常见问题', exact: true })).toHaveCount(0);
    const hero = page.locator('section[aria-labelledby="sceneturn-home-heading"]');
    await expect(hero.getByRole('link', { name: /试玩|体验/ })).toHaveCount(0);
    await expect(hero.getByRole('link', { name: 'GitHub' })).toHaveAttribute('href', 'https://github.com/Ethan3114/openfmv-workbench');
    await expect(page.locator('footer').getByRole('link', { name: '作品馆' })).toHaveAttribute('href', '/zh-CN/works');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

    await menu.focus();
    await page.keyboard.press('Enter');
    await expect(menu).toHaveAttribute('aria-expanded', 'true');
    await expect(header.getByRole('link', { name: 'GitHub' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toHaveAttribute('aria-expanded', 'false');
    await expect(menu).toBeFocused();
    await menu.click();
    await hero.getByRole('heading').click();
    await expect(menu).toHaveAttribute('aria-expanded', 'false');
  }
});

test('滚动更新阅读进度并揭示实际能力画面', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/zh-CN');
  const menu = page.getByRole('button', { name: '菜单' });
  const home = page.locator('[data-home]');
  await expect(menu).toContainText('0%');
  await expect(home).toHaveAttribute('data-home-motion', '');
  await expect(home).toHaveCSS('--home-hero-progress', '0');
  await page.evaluate(() => window.scrollTo(0, 420));
  await expect(home).toHaveCSS('--home-hero-progress', '1');
  await expect(menu).toContainText(/[1-9]\d*%/);
  await expect(page.getByRole('banner')).toHaveCSS('position', 'sticky');
  await page.getByRole('link', { name: '产品', exact: true }).click();
  await expect(page).toHaveURL(/#product$/);
  const cards = page.locator('#product article');
  for (const path of ['ai', 'codex']) {
    const image = cards.locator(`img[src="/sceneturn/home/${path}.webp"]`);
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate((element) => (element as HTMLImageElement).naturalWidth)).toBe(1440);
    await expect(image.locator('xpath=ancestor::article')).toHaveAttribute('data-home-revealed', '');
  }
  const productTop = await page.locator('#product').evaluate((element) => element.getBoundingClientRect().top);
  expect(productTop).toBeGreaterThanOrEqual(75);
  expect(productTop).toBeLessThanOrEqual(77);
  expect(errors).toEqual([]);
});

test('减少动态效果时保留内容、滚动进度和绘制比例', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/zh-CN');
  const artwork = page.locator('img[src="/sceneturn/tide.jpg"]').first().locator('..');
  await expect(artwork).toHaveCSS('transform', 'none');
  await page.getByRole('link', { name: '产品', exact: true }).click();
  await expect(page.locator('#product article').first()).toHaveCSS('opacity', '1');
  await expect(page.getByRole('button', { name: '菜单' })).toContainText(/[1-9]\d*%/);
});
