import { expect, test, type Page } from '@playwright/test';
import { createHash } from 'node:crypto';
import { mkdir, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { z } from 'zod';
import { enterManualStudio } from './manual-studio';

test.use({ viewport: { width: 1440, height: 1000 }, actionTimeout: 15000 });

async function createProject(page: Page) {
  await page.goto('/zh-CN/account'); await page.getByTestId('auth-mode-register').click();
  await page.getByTestId('auth-email').fill(`agent-browser-${crypto.randomUUID()}@example.com`);
  await page.getByTestId('auth-password').fill(crypto.randomUUID()); await page.getByTestId('auth-submit').click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.getByTestId('project-title').fill('角色任务与项目记忆验证'); await page.getByTestId('project-create').click();
  await enterManualStudio(page); await expect(page.getByTestId('canvas-add-agent')).toBeEnabled();
  const id = new URL(page.url()).searchParams.get('id'); if (!id) throw new Error('真实项目没有标识'); return id;
}
async function save(page: Page) { await page.getByTestId('canvas-save').click(); await expect(page.getByTestId('save-status')).toContainText('已保存'); }

async function realProfessionalText() {
  const directory = path.resolve('..', '.intermediate/codex-smoke');
  const sampleSchema = z.object({ artifact: z.object({ role: z.string() }), run: z.object({ targetIds: z.array(z.string()).min(1) }), canvas: z.object({ canvas: z.object({ nodes: z.array(z.object({ id: z.string(), metadata: z.object({ content: z.string().optional() }).passthrough() }).passthrough()) }) }) });
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const proof = JSON.parse(await readFile(path.join(directory, entry.name, 'proof.json'), 'utf8')) as { completed?: boolean; restartVerified?: boolean; revocationVerified?: boolean; sampleResultSha256?: string };
    if (!proof.completed || !proof.restartVerified || !proof.revocationVerified || !proof.sampleResultSha256) continue;
    const contents = await readFile(path.join(directory, entry.name, 'sample-result.json'));
    expect(createHash('sha256').update(contents).digest('hex')).toBe(proof.sampleResultSha256);
    const parsed = sampleSchema.safeParse(JSON.parse(contents.toString('utf8')));
    if (!parsed.success) continue;
    const content = parsed.data.canvas.canvas.nodes.find((node) => node.id === parsed.data.run.targetIds[0])?.metadata.content;
    if (content && content.length > 1000) return content;
  }
  throw new Error('需要先运行真实Codex专业产物验证，取得完整产物与重启证明。');
}

test('画布内角色任务、停止恢复、项目记忆与三种宽度可持续保存', async ({ page }) => {
  test.setTimeout(120000);
  const errors: string[] = []; page.on('pageerror', (error) => errors.push(error.message));
  const id = await createProject(page);
  await page.getByTestId('canvas-add-text').click();
  const text = page.getByTestId('canvas-node').last();
  const textId = await text.getAttribute('data-node-id'); if (!textId) throw new Error('真实文字节点没有标识');
  await text.getByTestId('canvas-node-body').dblclick(); await text.getByTestId('canvas-text').fill('角色林岚是一名修表师，随身携带一枚银色怀表。'); await save(page);
  await page.getByTestId('canvas-add-agent').click();
  const agent = page.locator('[data-agent-node]'); await expect(agent).toHaveCount(1);
  await expect(agent.getByTestId('agent-role').locator('option')).toHaveCount(6);
  await expect(agent.getByTestId('agent-delivery-mode')).toHaveValue('professional');
  await agent.getByTestId('agent-role').selectOption('writer'); await agent.getByTestId('agent-executor').selectOption('codex');
  await agent.getByTestId('agent-goal').fill('读取当前设定，将授权文字整理成一段剧本，保留林岚与银色怀表。');
  await agent.getByText('允许修改的现有节点', { exact: false }).click();
  await agent.getByTestId('agent-target').check(); await save(page);
  await agent.getByTestId('agent-start').click();
  await expect(agent.getByTestId('agent-status')).toHaveText('等待本人Codex连接');
  await expect(agent.getByTestId('agent-usage')).toContainText('账户费用：未知');
  const beforeStop = await page.request.get(`/api/v1/projects/${id}`); expect(beforeStop.status()).toBe(200);
  const revision = (await beforeStop.json()).revision;
  await agent.getByTestId('agent-goal').fill('下一次任务：在结尾加入钟声。');
  await agent.getByTestId('agent-stop').click(); await expect(agent.getByTestId('agent-status')).toHaveText('已停止');
  const afterStop = await page.request.get(`/api/v1/projects/${id}`); expect((await afterStop.json()).revision).toBe(revision);
  await expect(agent.getByTestId('agent-goal')).toHaveValue('下一次任务：在结尾加入钟声。');
  await agent.getByTestId('agent-resume').click(); await expect(agent.getByTestId('agent-status')).toHaveText('等待本人Codex连接');
  await agent.getByTestId('agent-stop').click(); await expect(agent.getByTestId('agent-status')).toHaveText('已停止');
  await agent.getByText('项目记忆与来源', { exact: true }).click();
  await agent.getByTestId('agent-memory-content').fill('林岚的怀表始终为银色，人物身份为修表师。'); await agent.getByTestId('agent-memory-save').click();
  await expect(agent.getByText('林岚的怀表始终为银色，人物身份为修表师。', { exact: true })).toBeVisible();
  await expect(agent.getByRole('button', { name: /来源 · 版本/ }).first()).toBeVisible();
  await agent.getByRole('button', { name: /来源 · 版本/ }).first().click();
  await expect(agent.getByTestId('agent-source')).toContainText('来源原文');
  await expect(agent.getByTestId('agent-source')).toContainText('角色任务与项目记忆验证');
  const memoriesResponse = await page.request.get(`/api/v1/projects/${id}/memories`); expect(memoriesResponse.status()).toBe(200);
  const memories = (await memoriesResponse.json()).memories; expect(memories).toHaveLength(1);
  expect(memories[0].sceneId).toBeNull(); expect(memories[0].branchId).toBeNull();
  await page.reload(); await expect(page.locator('[data-agent-node]')).toHaveCount(1);
  await expect(page.getByTestId('agent-goal')).toHaveValue('下一次任务：在结尾加入钟声。');
  await expect(page.getByTestId('agent-status')).toHaveText('已停止');
  await expect(page.locator(`[data-testid="canvas-node"][data-node-id="${textId}"]`)).toContainText('角色林岚');
  const directory = path.resolve('..', 'docs/evidence/阶段2-2'); await mkdir(directory, { recursive: true });
  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 }); await page.getByTestId('canvas-fit-all').click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1); expect(overflow).toBe(false);
    await page.screenshot({ path: path.join(directory, `创作助手-${width}px.png`), fullPage: true });
  }
  expect(errors).toEqual([]);
  const history = await page.request.get(`/api/v1/projects/${id}/agent-runs`); expect(history.status()).toBe(200);
  const { runs } = await history.json(); expect(runs).toHaveLength(1); expect(runs[0].calls).toHaveLength(0);
  expect(runs[0].targetIds).toEqual([textId]); expect(runs[0].status).toBe('stopped');
  expect(runs[0].deliveryMode).toBe('professional');
  page.once('dialog', async (dialog) => { expect(dialog.type()).toBe('confirm'); await dialog.accept(); });
  await page.locator('[data-agent-node]').getByTestId('canvas-node-delete').click(); await save(page);
  await page.getByTestId('canvas-add-agent').click();
  await expect(page.getByTestId('agent-status')).toHaveText('已停止');
  await page.getByTestId('agent-resume').click(); await expect(page.getByTestId('agent-status')).toHaveText('等待本人Codex连接');
  await page.getByTestId('agent-stop').click(); await expect(page.getByTestId('agent-status')).toHaveText('已停止');
  await page.setViewportSize({ width: 1440, height: 1000 }); await page.getByTestId('canvas-fit-all').click();
  const activeAgent = page.locator('[data-agent-node]');
  const agentId = await activeAgent.getAttribute('data-node-id'); if (!agentId) throw new Error('助手没有稳定标识');
  await activeAgent.getByTestId('agent-delivery-mode').selectOption('text_edit');
  await activeAgent.getByText('允许修改的现有节点', { exact: false }).click(); await activeAgent.getByTestId('agent-target').check();
  await activeAgent.getByTestId('canvas-node-header').click();
  await page.locator(`[data-testid="canvas-node"][data-node-id="${textId}"]`).getByTestId('canvas-node-header').click({ modifiers: ['ControlOrMeta'] });
  await page.getByTestId('canvas-copy-selection').click(); await save(page);
  const copiedResponse = await page.request.get(`/api/v1/projects/${id}/canvases/${runs[0].sceneId}`); expect(copiedResponse.status()).toBe(200);
  const copiedNodes = (await copiedResponse.json()).canvas.nodes as { id: string; type: string; metadata: { agentTargetIds?: string[]; agentDeliveryMode?: string } }[];
  const copiedAgent = copiedNodes.find((item) => item.type === 'agent' && item.id !== agentId);
  const copiedText = copiedNodes.find((item) => item.type === 'text' && item.id !== textId);
  expect(copiedAgent).toBeDefined(); expect(copiedText).toBeDefined();
  expect(copiedAgent?.metadata.agentTargetIds).toEqual([copiedText?.id]);
  expect(copiedAgent?.metadata.agentDeliveryMode).toBe('text_edit');
});

test('真实专业长文本候选可以编辑并保留其他候选', async ({ page }) => {
  const original = await realProfessionalText();
  const id = await createProject(page);
  const seeded = await page.evaluate(async ({ projectId, content }) => {
    const projectResponse = await fetch(`/api/v1/projects/${projectId}`);
    if (!projectResponse.ok) throw new Error('无法读取真实项目');
    const project = await projectResponse.json();
    const sceneId = project.project.graphData.nodes.find((node: { id: string; type: string }) => node.type === 'scene').id as string;
    const response = await fetch(`/api/v1/projects/${projectId}/canvases/${sceneId}`);
    if (!response.ok) throw new Error('无法读取真实画布');
    const envelope = await response.json();
    const nodeId = crypto.randomUUID(), firstId = crypto.randomUUID(), primaryId = crypto.randomUUID();
    const metadata = { content, status: 'success', primaryTextId: primaryId, texts: [{ id: firstId, content: '人工保留的另一份创作方案。', status: 'success' }, { id: primaryId, content, status: 'success' }] };
    const saved = await fetch(`/api/v1/projects/${projectId}/canvases/${sceneId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ connections: envelope.canvas.connections, viewport: envelope.canvas.viewport, expectedRevision: envelope.revision, operationId: crypto.randomUUID(), nodes: [...envelope.canvas.nodes, { id: nodeId, type: 'text', title: '真实专业长文本', position: { x: 100, y: 100 }, width: 600, height: 500, metadata }] }) });
    if (!saved.ok) throw new Error('无法保存实际文本候选');
    return { sceneId, nodeId, primaryId, firstId };
  }, { projectId: id, content: original });
  await page.goto(`/zh-CN/studio?id=${id}&sceneId=${seeded.sceneId}`);
  const node = page.locator(`[data-testid="canvas-node"][data-node-id="${seeded.nodeId}"]`);
  await expect(node).toBeVisible(); await node.getByTestId('canvas-node-body').dblclick();
  await expect(node.getByTestId('canvas-text')).toHaveValue(original);
  const revised = `${original}\n\n本人修订：保留各场次、分支和道具连续性要求。`;
  await node.getByTestId('canvas-text').fill(revised); await save(page);
  const response = await page.request.get(`/api/v1/projects/${id}/canvases/${seeded.sceneId}`);
  expect(response.status()).toBe(200);
  const saved = (await response.json()).canvas.nodes.find((item: { id: string }) => item.id === seeded.nodeId);
  expect(saved.metadata.content).toBe(revised); expect(saved.metadata.primaryTextId).toBe(seeded.primaryId);
  expect(saved.metadata.texts.find((item: { id: string }) => item.id === seeded.primaryId).content).toBe(revised);
  expect(saved.metadata.texts.find((item: { id: string }) => item.id === seeded.firstId).content).toBe('人工保留的另一份创作方案。');
  await page.reload(); await node.getByTestId('canvas-node-body').dblclick();
  await expect(node.getByTestId('canvas-text')).toHaveValue(revised);
});
