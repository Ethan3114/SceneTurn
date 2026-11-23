import { expect, test, type Locator, type Page } from '@playwright/test';
import { createProjectFromList } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 } });

async function bounds(locator: Locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error('画布上的操作对象没有显示');
  return box;
}

function port(canvas: Locator, id: string, side: 'source' | 'target') {
  return canvas.locator(`[data-testid="canvas-connection-${side}"][data-node-id="${id}"]`);
}

async function dragFrom(page: Page, start: Locator, destination: { x: number; y: number }) {
  const box = await bounds(start);
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(destination.x, destination.y, { steps: 12 });
  await page.mouse.up();
}

async function linePoints(line: Locator) {
  return line.evaluate((path: SVGPathElement) => {
    const matrix = path.getScreenCTM();
    if (!matrix) throw new Error('无法读取连线坐标');
    const length = path.getTotalLength();
    return [0, length / 2, length].map((distance) => {
      const { x, y } = path.getPointAtLength(distance).matrixTransform(matrix);
      return { x, y };
    });
  });
}

test('从端口点击或拖动打开菜单，曲线贴住卡片并保存连线', async ({ page }) => {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`canvas-link-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '画布连接验证');
  await expect(page).toHaveURL(/\/editor\?id=/);
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('创建的项目没有标识');

  await page.getByTitle('素材', { exact: true }).click();
  await page.getByTestId('materials-generate-tab').click();
  const workspace = page.getByTestId('material-workspace');
  const chat = page.getByTestId('material-chat');
  await expect(chat).toBeVisible();
  await chat.getByRole('button', { name: '关闭素材对话' }).click();
  await workspace.getByRole('button', { name: '手动编辑画布' }).click();
  const canvas = workspace.getByTestId('embedded-material-canvas');
  const addText = async () => {
    await canvas.getByTestId('canvas-add-node').click();
    await canvas.getByTestId('canvas-create-text').click();
  };
  await addText();
  const text = canvas.getByTestId('canvas-node').first();
  const textId = await text.getAttribute('data-node-id');
  if (!textId) throw new Error('文字节点没有标识');
  await text.getByTestId('canvas-node-body').dblclick();
  await text.getByTestId('canvas-text').fill('雾中小屋的分镜描述');
  await text.getByTestId('canvas-text').press('Escape');

  const canvasSurface = canvas.getByTestId('canvas-surface');
  const surface = await bounds(canvasSurface);
  const blank = await canvasSurface.evaluate((element) => {
    const box = element.getBoundingClientRect();
    for (const [horizontal, vertical] of [[.86, .6], [.87, .39], [.90, .76]]) {
      const x = box.x + box.width * horizontal;
      const y = box.y + box.height * vertical;
      const hit = document.elementFromPoint(x, y);
      if (hit && element.contains(hit) && !hit.closest('[data-node-id],[data-node-panel],[data-canvas-no-zoom],[data-connection-id]')) return { x, y };
    }
    throw new Error('画布右侧没有供连线使用的空白');
  });
  const output = port(canvas, textId, 'source');
  await page.mouse.click(blank.x, blank.y);
  await expect(output).toHaveCSS('opacity', '0');
  await output.hover();
  await expect(output).toHaveCSS('opacity', '1');
  await expect(output).toHaveAttribute('aria-label', '拖拽连线或点击选择节点类型');
  await expect(output).toHaveCSS('width', '24px');
  await expect(text.locator('.fmv-node-port[data-side="right"]')).toHaveCSS('width', '80px');
  await output.click();
  const menu = canvas.getByTestId('canvas-connection-create-menu');
  await expect(menu.getByTestId('canvas-linked-create-text')).toBeVisible();
  await expect(menu.getByTestId('canvas-linked-create-image')).toBeVisible();
  await expect(menu.getByTestId('canvas-linked-create-video')).toBeVisible();
  await expect(menu.getByTestId('canvas-linked-create-join')).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  await expect(canvas.getByTestId('canvas-node')).toHaveCount(1);
  await dragFrom(page, output, blank);
  await menu.getByTestId('canvas-linked-create-video').click();
  const configId = await canvas.getByTestId('canvas-node').last().getAttribute('data-node-id');
  if (!configId) throw new Error('视频节点没有标识');
  const config = canvas.locator(`[data-testid="canvas-node"][data-node-id="${configId}"]`);
  await expect(canvas.locator('[data-connection-id]')).toHaveCount(1);
  await canvas.getByTestId('canvas-fit-all').press('Enter');
  const hit = canvas.locator('[data-connection-id]').first();
  const firstWire = canvas.locator('.fmv-canvas-connection').first();
  const visibleLine = firstWire.locator('.fmv-canvas-connection-stroke');
  await expect(hit).toHaveCSS('stroke-width', '20px');
  await expect(visibleLine).toHaveCSS('stroke-width', '2px');
  await expect(visibleLine).toHaveCSS('stroke', 'rgb(134, 144, 156)');
  const [, midpoint] = await linePoints(hit);
  await page.mouse.move(midpoint.x, midpoint.y);
  await expect(visibleLine).toHaveCSS('stroke-width', '4px');
  await expect(visibleLine).toHaveCSS('stroke', 'rgb(192, 200, 208)');
  await expect(firstWire.locator('.fmv-canvas-connection-current path')).toHaveCount(8);
  const animation = firstWire.locator('.fmv-canvas-connection-current');
  await expect(animation).toHaveCSS('animation-duration', '3s');
  await expect(animation.locator('path').first()).toHaveCSS('stroke-opacity', '0.06');
  await page.mouse.click(midpoint.x, midpoint.y);
  await page.mouse.move(blank.x, surface.y + 10);
  await expect(visibleLine).toHaveCSS('stroke-width', '3px');
  await expect(visibleLine).toHaveCSS('stroke', 'rgb(224, 228, 232)');

  await canvas.getByRole('button', { name: '缩放选项' }).click();
  const zoom = canvas.getByRole('spinbutton', { name: '缩放百分比' });
  await zoom.fill('25');
  await zoom.press('Enter');
  const firstText = await bounds(text.getByTestId('canvas-node-body'));
  const separatePoint = await canvasSurface.evaluate((surface, anchor) => {
    const box = surface.getBoundingClientRect();
    const x = anchor.x;
    for (const y of [anchor.y - anchor.height - 38, anchor.y + anchor.height + 38]) {
      if (y < box.top + 24 || y + anchor.height > box.bottom - 36) continue;
      const hit = document.elementFromPoint(x, y);
      if (hit && surface.contains(hit) && !hit.closest('[data-node-id],[data-node-panel],[data-canvas-no-zoom],[data-connection-id]')) return { x, y };
    }
    throw new Error('文字卡片的同一列没有可用空位');
  }, firstText);
  await page.mouse.dblclick(separatePoint.x, separatePoint.y);
  await canvas.getByTestId('canvas-node-create-menu').getByTestId('canvas-create-text').click();
  const second = canvas.getByTestId('canvas-node').last();
  const secondId = await second.getAttribute('data-node-id');
  if (!secondId) throw new Error('第二个文字节点没有标识');
  await canvas.getByTestId('canvas-fit-all').press('Enter');
  const videoInput = port(canvas, configId, 'target');
  const secondPort = port(canvas, secondId, 'source');
  const waitForEndpoint = async (endpoint: Locator) => {
    await expect.poll(() => endpoint.evaluate((button) => {
      const box = button.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      if (button.contains(hit)) return '端口可点击';
      return `${button.getAttribute('data-testid')} (${Math.round(box.x + box.width / 2)}, ${Math.round(box.y + box.height / 2)}) 命中 ${hit?.closest('[data-testid]')?.getAttribute('data-testid') ?? hit?.tagName ?? '画布外'}`;
    })).toBe('端口可点击');
  };
  await videoInput.hover();
  await waitForEndpoint(videoInput);
  const videoInputBounds = await bounds(videoInput);
  await page.mouse.move(videoInputBounds.x + videoInputBounds.width / 2, videoInputBounds.y + videoInputBounds.height / 2);
  await page.mouse.down();
  await expect(canvas.locator('.fmv-canvas-connection-draft')).toHaveCount(1);
  const secondOutput = await bounds(secondPort);
  await page.mouse.move(secondOutput.x + secondOutput.width / 2, secondOutput.y + secondOutput.height / 2, { steps: 12 });
  await waitForEndpoint(secondPort);
  await page.mouse.up();
  await expect(canvas.locator('[data-connection-id]')).toHaveCount(2);

  await canvas.getByTestId('canvas-save').click();
  await expect(canvas.getByTestId('save-status')).toContainText('已保存');
  const response = await page.request.get(`/api/v1/projects/${id}`);
  expect(response.ok()).toBe(true);
  const project = await response.json();
  const saved = project.canvases.find((item: { nodes: { id: string }[] }) => item.nodes.some((node) => node.id === configId));
  expect(saved.nodes.find((node: { id: string }) => node.id === configId)).toMatchObject({ type: 'config', metadata: { generationMode: 'video', dramaTask: 'clip' } });
  expect(saved.connections).toMatchObject([
    { fromNodeId: textId, toNodeId: configId },
    { fromNodeId: secondId, toNodeId: configId },
  ]);

  for (const width of [1280, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await canvas.getByTestId('canvas-fit-all').press('Enter');
    const sourceCard = await bounds(text.getByTestId('canvas-node-body'));
    const targetCard = await bounds(config.getByTestId('canvas-node-body'));
    const outputPoint = await bounds(output);
    const inputPoint = await bounds(port(canvas, configId, 'target'));
    const [start, , end] = await linePoints(hit);
    expect(start.x).toBeCloseTo(sourceCard.x + sourceCard.width, 0);
    expect(start.y).toBeCloseTo(sourceCard.y + sourceCard.height / 2, 0);
    expect(end.x).toBeCloseTo(targetCard.x, 0);
    expect(end.y).toBeCloseTo(targetCard.y + targetCard.height / 2, 0);
    expect((outputPoint.x + outputPoint.width / 2 - start.x) / (outputPoint.width / 24)).toBeCloseTo(20, 0);
    expect((end.x - inputPoint.x - inputPoint.width / 2) / (inputPoint.width / 24)).toBeCloseTo(20, 0);
  }
});
