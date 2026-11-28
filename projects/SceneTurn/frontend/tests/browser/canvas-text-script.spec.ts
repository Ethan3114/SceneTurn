import { expect, test, type Page } from '@playwright/test';
import path from 'node:path';
import { addCanvasNode, createProjectFromList, enterManualStudio } from './manual-studio';

test.use({ viewport: { width: 1600, height: 1100 } });

async function openCanvas(page: Page) {
  await page.goto('/zh-CN/account');
  await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`canvas-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID());
  await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await createProjectFromList(page, '文字和分镜功能验证');
  await enterManualStudio(page);
  const id = new URL(page.url()).searchParams.get('id');
  return async () => {
    await page.getByTestId('canvas-save').click();
    await expect(page.getByTestId('save-status')).toContainText('已保存');
    const response = await page.request.get(`/api/v1/projects/${id}`);
    expect(response.ok()).toBe(true);
    const { canvases } = await response.json();
    return canvases[0] as { nodes: { id: string; title: string; type: string; metadata: Record<string, unknown> }[]; connections: { fromNodeId: string; toNodeId: string }[] };
  };
}

test('文字快捷操作保存正文、参考图及视频音乐连线', async ({ page }) => {
  const save = await openCanvas(page);
  const first = await addCanvasNode(page, 'text');
  await first.getByRole('button', { name: '自己编写内容' }).click();
  await first.getByTestId('canvas-text').fill('少年从山路回望古城。');
  await first.getByTestId('canvas-text').press('Escape');
  await expect(first.getByText('少年从山路回望古城。', { exact: true })).toBeVisible();
  const second = await addCanvasNode(page, 'text');
  const secondId = await second.getAttribute('data-node-id');
  await second.getByRole('button', { name: '文生视频' }).click();
  const third = await addCanvasNode(page, 'text');
  const thirdId = await third.getAttribute('data-node-id');
  await third.getByRole('button', { name: '文字生音乐' }).click();
  const fourth = await addCanvasNode(page, 'text');
  const fourthId = await fourth.getAttribute('data-node-id');
  await fourth.getByRole('button', { name: '图片反推提示词' }).click();
  const target = page.locator(`[data-node-id="${fourthId}"]`).first();
  await expect(target.getByTestId('text-reference')).toHaveCount(1);
  await expect(target.getByTestId('generation-prompt')).toContainText('光影');
  await target.getByRole('button', { name: '模型', exact: true }).click();
  await target.getByRole('option', { name: 'DeepSeek Flash' }).click();
  await expect(target.getByTestId('text-generation-start')).toBeDisabled();
  await target.getByRole('button', { name: '模型', exact: true }).click();
  await target.getByRole('option', { name: /Qwen/ }).click();
  const canvas = await save();
  for (const [source, type] of [[secondId, 'config'], [thirdId, 'audio']]) {
    const destination = canvas.connections.find((edge) => edge.fromNodeId === source);
    expect(canvas.nodes.find((item) => item.id === destination?.toNodeId)?.type).toBe(type);
  }
  expect(canvas.nodes.some((item) => item.type === 'image' && item.metadata.storageKey)).toBe(true);
  expect(canvas.connections.some((edge) => edge.toNodeId === fourthId)).toBe(true);
});

test('剧本作为上游，多镜同时建立独立的图像任务，旧版保留单独的镜头表', async ({ page }) => {
  const save = await openCanvas(page);
  const initial = await addCanvasNode(page, 'script');
  const id = await initial.getAttribute('data-node-id');
  const script = page.locator(`[data-node-id="${id}"]`).first();
  await script.getByRole('button', { name: '剧本生成分镜脚本' }).click();
  await expect(script.getByTestId('text-reference')).toHaveCount(1);
  await script.getByRole('button', { name: '自己编写分镜脚本' }).click();
  const table = page.getByTestId('canvas-script-table');
  await table.getByLabel('镜头 1 画面描述').fill('一座青石拱桥。');
  await table.getByTestId('canvas-script-add-row').click();
  await table.getByLabel('镜头 2 画面描述').fill('船穿过桥洞。');
  await table.getByRole('button', { name: '下一步：准备资产' }).click();
  await table.getByRole('checkbox', { name: '选择全部镜头' }).check();
  await table.getByRole('button', { name: '生成分镜图' }).click();
  await page.getByTestId('canvas-add-node').click();
  await page.getByTestId('canvas-create-script-menu').click();
  await page.getByTestId('canvas-create-script-legacy').click();
  const legacy = page.getByTestId('canvas-node').last();
  await legacy.getByRole('button', { name: '自己编写分镜脚本' }).click();
  await expect(table.getByRole('columnheader', { name: '动作' })).toBeVisible();
  await table.getByLabel('镜头 1 动作').fill('撑篙');
  await table.getByRole('button', { name: '完成' }).click();
  const snapshot = await save();
  expect(snapshot.nodes.filter((node) => node.metadata.dramaTask === 'shotImage')).toHaveLength(2);
  expect(snapshot.nodes.some((node) => node.metadata.scriptVersion === 'legacy' && String(node.metadata.content).includes('撑篙'))).toBe(true);
});

test('取消素材库勾选后分镜面板保留当前焦点和已选参考', async ({ page }) => {
  await openCanvas(page);
  const script = await addCanvasNode(page, 'script');
  const id = await script.getAttribute('data-node-id');
  const card = page.locator(`[data-node-id="${id}"]`).first();
  await card.getByRole('button', { name: '角色生成分镜脚本' }).click();
  await expect(card.getByTestId('text-reference')).toHaveCount(2);
  await expect(card.getByTestId('generation-prompt')).toContainText('林芽');
  await card.getByRole('button', { name: '添加参考' }).click();
  await card.getByRole('button', { name: '个人素材库' }).click();
  const library = page.getByRole('dialog', { name: '个人素材库' });
  await library.getByRole('checkbox').first().check();
  await library.getByRole('button', { name: /取\s*消/ }).click();
  await expect(card.getByTestId('text-reference')).toHaveCount(2);
});

test('分镜图支持真实上传与调整顺序，视频逐镜携带剧本与图片并保存', async ({ page }) => {
  const save = await openCanvas(page);
  const script = await addCanvasNode(page, 'script');
  const id = await script.getAttribute('data-node-id');
  const card = page.locator(`[data-node-id="${id}"]`).first();
  await card.getByRole('button', { name: '角色生成分镜脚本' }).click();
  await expect(card.getByTestId('text-reference')).toHaveCount(2);
  const file = path.resolve('public/sceneturn/canvas/reverse-image.png');
  await card.locator('input[type=file]').setInputFiles(file);
  await expect(card.getByTestId('text-reference')).toHaveCount(3);
  await card.getByTestId('text-reference').last().press('ArrowLeft');
  await expect(card.getByTestId('text-reference').nth(1)).toContainText('2');
  await card.getByRole('button', { name: '移除参考 reverse-image.png' }).click({ force: true });
  await expect(card.getByTestId('text-reference')).toHaveCount(2);
  await card.getByRole('button', { name: '自己编写分镜脚本' }).click();
  const table = page.getByTestId('canvas-script-table');
  await table.getByLabel('镜头 1 名称').fill('古城');
  await table.getByLabel('镜头 1 画面描述').fill('少年从山路回望古城。');
  await table.getByLabel('镜头 1 时长（秒）').fill('4');
  await table.getByRole('button', { name: '下一步：准备资产' }).click();
  await table.getByRole('combobox', {name: '镜头 1 分镜图'}).selectOption({index: 1});
  await table.getByRole('button', { name: '下一步：合成提示词' }).click();
  await table.getByRole('checkbox', { name: '选择镜头 1' }).check();
  await table.getByRole('button', { name: '分镜转视频' }).click();
  const saved = await save();
  const video = saved.nodes.find((node) => node.metadata.dramaTask === 'clip');
  const incoming = saved.connections.filter((link) => link.toNodeId === video?.id).map((link) => saved.nodes.find((n) => n.id === link.fromNodeId));
  expect(incoming.some((node) => node?.type === 'text' && String(node.metadata.content).includes('少年从山路回望古城。'))).toBe(true);
  expect(incoming.some((node) => node?.type === 'image' && node.id === video?.metadata.firstFrameNodeId)).toBe(true);
  expect(video?.metadata.seconds).toBe('4');
  await page.reload();
  await page.getByRole('button', { name: '显示全部节点' }).click();
  await card.getByRole('button', { name: /打开脚本节点/ }).click();
  await expect(table.getByLabel('镜头 1 名称')).toHaveValue('古城');
});
