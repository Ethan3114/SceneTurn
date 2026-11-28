import { expect, test, type Locator, type Page } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

import { createProjectFromList, enterManualStudio, addCanvasNode } from './manual-studio';

type ProbeWindow = Window & { canvasInputDelay?: Promise<number> };

async function openText(node: Locator) {
  const editor = node.getByTestId('canvas-text');
  if (!(await editor.isVisible())) await node.getByTestId('canvas-node-body').dblclick();
  await expect(editor).toBeVisible();
  return editor;
}

async function save(page: Page) {
  await page.getByTestId('canvas-save').click();
  await expect(page.getByTestId('save-status')).toHaveText(/已保存/);
}

async function measureEditor(editor: Locator, page: Page, initial: string) {
  await editor.press('End');
  let content = initial;
  const samples: number[] = [];
  for (const character of 'abcdefghijklmnopqrstuvwxyz1234') {
    content += character;
    await editor.evaluate((input, expected) => {
      if (!(input instanceof HTMLTextAreaElement)) throw new Error('画布未显示正文输入框');
      (window as ProbeWindow).canvasInputDelay = new Promise((resolve, reject) => {
        const measure = () => {
          const started = performance.now();
          requestAnimationFrame(() => requestAnimationFrame(() => {
            clearTimeout(deadline);
            if (input.value !== expected) reject(new Error('键盘输入与画布正文不一致'));
            else resolve(performance.now() - started);
          }));
        };
        const deadline = setTimeout(() => {
          input.removeEventListener('input', measure);
          reject(new Error('未收到实际键盘输入'));
        }, 5000);
        input.addEventListener('input', measure, { once: true });
      });
    }, content);
    await editor.pressSequentially(character);
    samples.push(await page.evaluate(async () => {
      const duration = await (window as ProbeWindow).canvasInputDelay;
      if (duration === undefined) throw new Error('没有测得输入响应时间');
      delete (window as ProbeWindow).canvasInputDelay;
      return duration;
    }));
  }
  const sorted = [...samples].sort((a, b) => a - b);
  return { content, samples, p95Milliseconds: sorted[Math.ceil(sorted.length * .95) - 1] };
}

for (const width of [1280, 1440]) {
  test(`桌面 ${width} 像素键入响应、并行保存及断网恢复`, async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: 930 });
    await page.goto('/zh-CN/account');
    await page.getByTestId('auth-mode-register').click();
    await page.getByTestId('auth-email').fill(`save-recovery-${crypto.randomUUID()}@example.com`);
    await page.getByTestId('auth-password').fill(crypto.randomUUID());
    await page.getByTestId('auth-submit').click();
    await expect(page).toHaveURL(/\/projects$/);
    await createProjectFromList(page, '桌面画布保存验证');
    await enterManualStudio(page);
    const projectUrl = page.url();
    const projectId = new URL(projectUrl).searchParams.get('id');
    const first = await addCanvasNode(page, 'text');
    const firstId = await first.getAttribute('data-node-id');
    if (!projectId || !firstId) throw new Error('实际项目或画布节点缺少标识');
    const firstNode = page.locator(`[data-testid="canvas-node"][data-node-id="${firstId}"]`);
    const initialText = '前四秒的画面';
    const editor = await openText(firstNode);
    await editor.fill(initialText);
    const measured = await measureEditor(editor, page, initialText);
    expect(measured.p95Milliseconds).toBeLessThanOrEqual(200);
    await writeFile(testInfo.outputPath('画布输入响应.json'), JSON.stringify({ width, ...measured }));
    const second = await addCanvasNode(page, 'text');
    const secondId = await second.getAttribute('data-node-id');
    if (!secondId) throw new Error('第二个节点缺少标识');
    await (await openText(second)).fill('另一窗口将调整此节点');
    await save(page);
    const original = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
    expect(original.canvases[0].nodes.find((node: { id: string }) => node.id === firstId).metadata.content).toBe(measured.content);

    const other = await context.newPage();
    await other.setViewportSize({ width, height: 930 });
    await other.goto(projectUrl);
    const otherFirst = other.locator(`[data-testid="canvas-node"][data-node-id="${firstId}"]`);
    const otherSecond = other.locator(`[data-testid="canvas-node"][data-node-id="${secondId}"]`);
    await other.getByTestId('canvas-fit-all').click();
    await (await openText(otherSecond)).fill('另一窗口已保存的台词');
    await save(other);
    await page.getByTestId('canvas-fit-all').click();
    await (await openText(firstNode)).fill('当前窗口保留的分镜');
    await save(page);
    await expect(page.getByText('其他窗口的修改已合并，画布已保存。')).toBeVisible();
    const localSecond = page.locator(`[data-testid="canvas-node"][data-node-id="${secondId}"]`);
    await expect(localSecond).toContainText('另一窗口已保存的台词');

    await context.setOffline(true);
    await (await openText(firstNode)).fill('断网时仍能编辑的草稿');
    await page.getByTestId('canvas-save').click();
    await expect(page.getByTestId('save-status')).toContainText('有未保存');
    await expect(page.getByTestId('canvas-save')).toBeEnabled();
    await context.setOffline(false);
    await save(page);

    await other.reload();
    await other.getByTestId('canvas-fit-all').click();
    await (await openText(otherFirst)).fill('另一窗口更新了同一段');
    await save(other);
    await (await openText(firstNode)).fill('此窗口尚未同步的分镜');
    await page.getByTestId('canvas-save').click();
    const conflict = page.getByRole('alert').filter({ hasText: '相同的画布内容' });
    await expect(conflict).toContainText('当前草稿已保留');
    await expect(await openText(firstNode)).toHaveValue('此窗口尚未同步的分镜');
    await expect(page.getByTestId('save-status')).toContainText('有未保存');
    const persisted = await (await page.request.get(`/api/v1/projects/${projectId}`)).json();
    expect(persisted.canvases[0].nodes.find((node: { id: string }) => node.id === firstId).metadata.content)
      .toBe('另一窗口更新了同一段');
    await other.close();
  });
}
