import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 1440, height: 900 } });

test('四项工作台预览可用键盘和指针切换', async ({ page }) => {
  await page.goto('/zh-CN');

  const features = page.locator('#features');
  const tabs = features.getByRole('tab');
  const preview = features.getByRole('tabpanel');
  const graph = features.getByRole('tab', { name: '剧情画布', exact: true });
  const timeline = features.getByRole('tab', { name: '场景与互动', exact: true });
  const player = features.getByRole('tab', { name: '网页试玩', exact: true });
  const assets = features.getByRole('tab', { name: '项目与素材', exact: true });

  await expect(tabs).toHaveCount(4);
  await expect(graph).toHaveAttribute('aria-selected', 'true');
  await expect(preview).toHaveAccessibleName('剧情画布');

  await graph.press('ArrowDown');
  await expect(timeline).toBeFocused();
  await expect(timeline).toHaveAttribute('aria-selected', 'true');
  await expect(preview).toHaveAccessibleName('场景与互动');
  await expect.poll(() => preview.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await expect(preview).toHaveCSS('animation-duration', '0.18s');

  await timeline.press('End');
  await expect(assets).toBeFocused();
  await expect(preview).toHaveAccessibleName('项目与素材');

  await assets.press('ArrowDown');
  await expect(graph).toBeFocused();

  await player.click();
  await expect(preview).toHaveAccessibleName('网页试玩');
  await expect.poll(() => preview.getByRole('img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await expect(features.locator('[role="tab"][aria-selected="true"]')).toHaveCount(1);
});

test('常见问题一次展开一项并可用键盘收起', async ({ page }) => {
  await page.goto('/zh-CN');

  const faq = page.locator('#faq');
  const questions = faq.getByRole('button');
  const visibleAnswer = faq.getByRole('region');
  const first = faq.getByRole('button', { name: '可以用手头的素材开始吗？', exact: true });
  const codex = faq.getByRole('button', { name: '怎样让本机 Codex 编排剧情？', exact: true });
  const settings = faq.getByRole('link', { name: '查看模型设置', exact: true });
  await expect(questions).toHaveCount(5);
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await expect(visibleAnswer).toContainText('导入项目');
  await expect(settings).toHaveAttribute('href', '/zh-CN/settings/models');

  await codex.press('Enter');
  await expect(first).toHaveAttribute('aria-expanded', 'false');
  await expect(codex).toHaveAttribute('aria-expanded', 'true');
  await expect(codex).toBeFocused();
  await expect(visibleAnswer).toHaveCount(1);
  await expect(visibleAnswer).toContainText('Codex CLI');
  await expect(visibleAnswer).toHaveCSS('transition-duration', '0.2s, 0s');

  await codex.press('Space');
  await expect(codex).toHaveAttribute('aria-expanded', 'false');
  await expect(visibleAnswer).toHaveCount(0);
});

test('减少动态效果时功能切换和问答即时显示', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/zh-CN');

  const features = page.locator('#features');
  await features.getByRole('tab', { name: '场景与互动' }).click();
  await expect(features.getByRole('tabpanel')).toHaveCSS('animation-name', 'none');

  const faq = page.locator('#faq');
  await faq.getByRole('button', { name: '怎样让本机 Codex 编排剧情？' }).press('Enter');
  await expect(faq.getByRole('region')).toHaveCSS('transition-duration', '0s');
  await expect(faq.getByRole('region')).toContainText('Codex CLI');
});
