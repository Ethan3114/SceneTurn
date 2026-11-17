import { expect, type Page } from '@playwright/test';

export async function enterManualStudio(page: Page) {
  await expect(page).toHaveURL(/\/editor\?id=/);
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('真实项目没有项目标识');
  await page.goto(`/zh-CN/studio?id=${id}`);
  await expect(page.getByTestId('canvas-add-config')).toBeEnabled();
}
