import { expect, type Page } from '@playwright/test';

export async function createProjectFromList(page: Page, title: string) {
  await page.getByTestId('project-new').click();
  const dialog = page.getByRole('dialog', { name: '新建项目' });
  await dialog.getByTestId('project-title').fill(title);
  await dialog.getByTestId('project-create').click();
}

export async function enterManualStudio(page: Page) {
  await expect(page).toHaveURL(/\/editor\?id=/);
  const id = new URL(page.url()).searchParams.get('id');
  if (!id) throw new Error('真实项目没有项目标识');
  await page.goto(`/zh-CN/studio?id=${id}`);
  await expect(page.getByTestId('canvas-add-node')).toBeEnabled();
}

export async function addCanvasNode(page: Page, kind: 'text' | 'script' | 'image' | 'video' | 'smart-edit' | 'audio') {
  const nodes = page.getByTestId('canvas-node');
  const before = await nodes.count();
  await page.getByTestId('canvas-add-node').click();
  if (kind === 'script') await page.getByTestId('canvas-create-script-menu').click();
  await page.getByTestId(`canvas-create-${kind}`).click();
  await expect(nodes).toHaveCount(before + 1);
  return nodes.last();
}

export async function restoreLegacyCanvasAssistant(page: Page) {
  const url = new URL(page.url());
  const projectId = url.searchParams.get('id');
  if (!projectId) throw new Error('真实项目没有项目标识');
  const response = await page.request.get(`/api/v1/projects/${projectId}`);
  expect(response.ok()).toBe(true);
  const envelope = await response.json();
  const sceneId = url.searchParams.get('sceneId') ?? envelope.project.graphData.nodes.find((node: { type: string }) => node.type === 'scene')?.id;
  if (!sceneId) throw new Error('真实项目没有剧情场景');
  const current = await page.request.get(`/api/v1/projects/${projectId}/canvases/${sceneId}`);
  expect(current.ok()).toBe(true);
  const { canvas, revision } = await current.json();
  const id = crypto.randomUUID();
  const node = { id, type: 'agent', title: '素材创作助手', position: { x: 490, y: 85 }, width: 460, height: 720, metadata: { agentWorkspace: 'scene', agentRole: 'writer', agentGoal: '', agentExecutor: 'platform', agentTargetIds: [] } };
  const saved = await page.request.put(`/api/v1/projects/${projectId}/canvases/${sceneId}`, { data: {
    nodes: [...canvas.nodes, node], connections: canvas.connections, viewport: canvas.viewport,
    expectedRevision: revision, operationId: crypto.randomUUID(),
  } });
  expect(saved.ok(), saved.ok() ? undefined : await saved.text()).toBe(true);
  await page.reload();
  await expect(page.locator(`[data-agent-node][data-node-id="${id}"]`)).toBeVisible();
}
