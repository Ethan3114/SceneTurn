import { expect, test } from '@playwright/test';

import { createProjectFromList } from './manual-studio';

test('桌面试玩保留播放器焦点，退出后回到原入口', async ({ page }) => {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`player-keyboard-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '试玩键盘操作验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('真实项目没有项目标识');
  const editedTitle = '试玩前保存的项目名称';

  for (const [route, width] of [['editor', 1280], ['nodes', 1440]] as const) {
    await test.step(`${width} 像素宽度的 ${route} 试玩`, async () => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(`/zh-CN/${route}?id=${id}`);
      const scene = page.locator('.react-flow__node').filter({ has: page.getByRole('textbox', { name: '场景', exact: true }) });
      if (route === 'editor') {
        await scene.press('Enter');
        await expect(scene).toHaveClass(/\bselected\b/);
      } else {
        await page.getByRole('textbox', { name: '项目名称' }).fill(editedTitle);
      }
      const preview = page.getByRole('button', { name: '试玩', exact: true });
      await expect(preview).toBeEnabled();
      await preview.focus();
      await page.keyboard.press('Enter');

      const player = page.getByRole('dialog', { name: '互动游戏播放器' });
      const exit = player.getByRole('button', { name: '退出', exact: true });
      await expect(exit).toBeFocused();
      await expect(preview).toHaveCount(0);
      if (route === 'nodes') {
        const response = await page.request.get(`/api/v1/projects/${id}`);
        expect(response.ok()).toBe(true);
        expect((await response.json()).project.title).toBe(editedTitle);
      }
      for (const key of ['Shift+Tab', 'Tab', 'Tab', 'Tab', 'Tab', 'Tab']) {
        await page.keyboard.press(key);
        expect(await player.evaluate((dialog) => dialog.contains(document.activeElement))).toBe(true);
      }
      await page.keyboard.press('Delete');

      await page.keyboard.press('Escape');
      await expect(player).toHaveCount(0);
      await expect(preview).toBeFocused();
      if (route === 'editor') await expect(scene).toBeVisible();

      await page.keyboard.press('Enter');
      await expect(exit).toBeFocused();
      await page.keyboard.press('Enter');
      await expect(player).toHaveCount(0);
      await expect(preview).toBeFocused();
    });
  }
});
