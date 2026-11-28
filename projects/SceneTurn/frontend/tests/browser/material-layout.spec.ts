import { expect, test, type Locator } from '@playwright/test';

async function bounds(locator: Locator) {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  if (!box) throw new Error('可见的素材面板没有布局尺寸');
  return { ...box, right: box.x + box.width, bottom: box.y + box.height };
}

test('生成素材占满创作台，岗位成果分栏并保留对话与手动画布操作', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`material-layout-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);

  const response = await page.request.post('/api/v1/projects', { data: { title: '生成素材布局验证' } });
  expect(response.status()).toBe(201);
  const { project: { id } } = await response.json();
  await page.goto(`/zh-CN/editor?id=${id}`);
  await page.getByTitle('素材', { exact: true }).click();
  const materials = page.getByTestId('editor-materials');
  const navigation = page.getByTestId('material-project-nav');
  const tabs = page.getByTestId('materials-rail');
  const generateTab = materials.getByTestId('materials-generate-tab');
  await generateTab.click();
  await expect(generateTab).toHaveAttribute('aria-pressed', 'true');

  const workspace = materials.getByTestId('material-workspace');
  const canvas = workspace.getByTestId('material-role-canvas');
  const scene = workspace.getByTestId('material-scene-toolbar');
  const chat = materials.getByTestId('material-chat');
  const prompt = chat.getByRole('textbox', { name: '素材需求' });
  const picker = chat.getByTestId('material-model-picker');
  await expect(prompt).toBeEditable();
  await expect(chat).toHaveCSS('background-color', 'rgb(25, 25, 25)');
  await expect(canvas.locator('[data-role]')).toHaveCount(6);
  await expect(canvas.getByTestId('material-artifact')).toHaveCount(0);

  for (const { width, height } of [{ width: 1440, height: 900 }, { width: 1280, height: 800 }]) {
    await page.setViewportSize({ width, height });
    const [projectHeader, rail, area, roles, controls, conversation, composer] = await Promise.all([
      bounds(navigation), bounds(tabs), bounds(materials), bounds(canvas), bounds(scene), bounds(chat), bounds(prompt),
    ]);
    expect(projectHeader.y).toBeLessThanOrEqual(2);
    expect(projectHeader.right).toBeGreaterThanOrEqual(width - 2);
    expect(rail.x).toBeLessThanOrEqual(2);
    expect(rail.right).toBeGreaterThanOrEqual(width - 2);
    expect(rail.y).toBeGreaterThanOrEqual(52);
    expect(rail.y).toBeLessThanOrEqual(68);
    expect(Math.abs(rail.y - projectHeader.bottom)).toBeLessThanOrEqual(2);
    expect(rail.height).toBeGreaterThanOrEqual(60);
    expect(rail.height).toBeLessThanOrEqual(72);
    expect(area.x).toBeLessThanOrEqual(2);
    expect(area.right).toBeGreaterThanOrEqual(width - 2);
    expect(area.bottom).toBeGreaterThanOrEqual(height - 2);
    expect(Math.abs(conversation.y - rail.bottom)).toBeLessThanOrEqual(2);
    expect(Math.abs(conversation.y - controls.y)).toBeLessThanOrEqual(2);
    expect(controls.right).toBeLessThanOrEqual(conversation.x + 2);
    expect(roles.right).toBeLessThanOrEqual(conversation.x + 2);
    expect(roles.y).toBeGreaterThanOrEqual(controls.bottom - 2);
    expect(conversation.right).toBeGreaterThanOrEqual(width - 2);
    expect(conversation.bottom).toBeGreaterThanOrEqual(height - 2);
    expect(conversation.width / width).toBeGreaterThan(0.25);
    expect(conversation.width / width).toBeLessThan(0.33);
    if (width === 1440) expect(Math.abs(conversation.width - 404)).toBeLessThanOrEqual(18);
    expect(composer.bottom).toBeLessThanOrEqual(height);
    expect(composer.y).toBeGreaterThan(height - 200);
    expect(Math.abs(composer.height - 64)).toBeLessThanOrEqual(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    const [editor, writer, sceneDesigner, character, storyboard, video] = await Promise.all(
      ['director', 'writer', 'scene_designer', 'character_designer', 'storyboarder', 'media_producer']
        .map((role) => bounds(canvas.locator(`[data-role="${role}"]`))),
    );
    expect(editor.y).toBeLessThan(writer.y);
    expect(editor.width).toBeGreaterThan(roles.width * 0.88);
    expect(writer.x).toBeLessThan(sceneDesigner.x);
    expect(sceneDesigner.x).toBeLessThan(character.x);
    expect(Math.abs(writer.y - sceneDesigner.y)).toBeLessThanOrEqual(2);
    expect(Math.abs(writer.y - character.y)).toBeLessThanOrEqual(2);
    expect(storyboard.y).toBeGreaterThan(writer.y);
    expect(Math.abs(storyboard.y - video.y)).toBeLessThanOrEqual(2);
    expect(storyboard.right).toBeLessThanOrEqual(video.x + 2);
    expect(storyboard.width).toBeGreaterThan(writer.width * 1.8);
  }

  const draft = '调整素材中的机位与光线';
  await prompt.fill(draft);
  await picker.focus();
  await page.keyboard.press('Enter');
  const models = page.getByRole('dialog', { name: '新一轮模型' });
  await expect(models.getByRole('radio', { name: '个人 Codex' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(models).not.toBeVisible();
  await expect(picker).toBeFocused();
  await expect(prompt).toHaveValue(draft);

  await workspace.getByRole('button', { name: '手动编辑画布' }).click();
  await expect(workspace.getByTestId('embedded-material-canvas')).toBeVisible();
  await workspace.getByRole('button', { name: '返回岗位展示' }).click();
  await expect(canvas).toBeVisible();
  await expect(prompt).toHaveValue(draft);
  await materials.getByTestId('materials-library-tab').click();
  await expect(materials.getByTestId('materials-library-tab')).toHaveAttribute('aria-pressed', 'true');
  await expect(chat).not.toBeVisible();
  await navigation.getByRole('button', { name: '问问 AI' }).click();
  await expect(generateTab).toHaveAttribute('aria-pressed', 'true');
  await expect(chat).toBeVisible();
  await navigation.getByRole('button', { name: '剧情编排' }).click();
  await expect(materials).not.toBeVisible();
  await expect(page.getByTitle('素材', { exact: true })).toBeVisible();

  const projectViews = page.getByRole('navigation', { name: '项目视图' });
  await expect(projectViews.getByRole('button', { name: '剧情编排' })).toHaveAttribute('aria-current', 'page');
  await projectViews.getByRole('button', { name: '时间线' }).click();
  await expect(page).toHaveURL(/\/nodes\?/);
  await expect(projectViews.getByRole('button', { name: '时间线' })).toHaveAttribute('aria-current', 'page');
  await projectViews.getByRole('button', { name: '素材' }).click();
  await expect(page).toHaveURL(/\/editor\?/);
  await expect(materials).toBeVisible();
  await expect(navigation.getByRole('button', { name: '素材' })).toHaveAttribute('aria-current', 'page');
  expect(errors).toEqual([]);
});
