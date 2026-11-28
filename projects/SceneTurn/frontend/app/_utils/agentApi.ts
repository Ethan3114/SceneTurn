import { z } from 'zod';

import { api, operationId } from './cloudApi';

const roleSchema = z.object({ id: z.string(), title: z.string(), description: z.string(), tools: z.array(z.string()), available: z.boolean().optional(), reason: z.string().nullable().optional() }).passthrough();
const stepSchema = z.object({ id: z.string().optional(), title: z.string().optional(), role: z.string().optional(), status: z.string().optional(), tool: z.string().nullable().optional(), toolName: z.string().optional(), result: z.unknown().optional(), error: z.unknown().optional() }).passthrough();
const usageSchema = z.object({ inputTokens: z.number().nullable().optional(), outputTokens: z.number().nullable().optional(), totalTokens: z.number().nullable().optional(), costMicros: z.number().nullable().optional(), reservedCostMicros: z.number().nullable().optional() }).passthrough();
const runSchema = z.object({ id: z.string(), projectId: z.string(), sceneId: z.string(), branchId: z.string().nullable().optional(), controllerNodeId: z.string().nullable().optional(), role: z.string(), executor: z.string(), deliveryMode: z.enum(['professional', 'text_edit', 'tool_action', 'legacy']).nullable().optional(), artifactContractVersion: z.string().nullable().optional(), goal: z.string(), status: z.string(), projectRevision: z.number(), currentRevision: z.number().optional(), targetIds: z.array(z.string()), plan: z.unknown().optional(), steps: z.array(stepSchema), calls: z.array(z.unknown()).optional(), result: z.unknown().optional(), error: z.unknown().optional(), usage: usageSchema, createdAt: z.string().optional(), updatedAt: z.string().optional() }).passthrough();
const connectionSchema = z.object({ id: z.string(), projectId: z.string(), tools: z.array(z.string()), clientVersion: z.string().nullable().optional(), status: z.string(), canGenerateImages: z.boolean().optional(), expiresAt: z.string(), lastSeenAt: z.string().nullable().optional(), createdAt: z.string() }).passthrough();
const sourceSchema = z.object({ kind: z.string(), id: z.string(), revision: z.number(), sceneId: z.string().nullable().optional() }).passthrough();
const memorySchema = z.object({ id: z.string(), kind: z.string(), content: z.string(), sceneId: z.string().nullable().optional(), branchId: z.string().nullable().optional(), sources: z.array(sourceSchema), valid: z.boolean().optional() }).passthrough();

export type AgentRole = z.infer<typeof roleSchema>;
export const agentRunSchema = runSchema.extend({ limits: z.object({ maxSteps: z.number() }).passthrough().optional(), initialRevision: z.number().optional(), canResume: z.boolean().optional(), canExtendLimit: z.boolean().optional(), retryableUnknownCallId: z.string().nullable().optional(), plan: z.array(z.object({ role: z.string(), title: z.string(), instruction: z.string() }).passthrough()) });
export type AgentRun = z.infer<typeof runSchema>;
export type ProjectMemory = z.infer<typeof memorySchema>;
export type CodexConnection = z.infer<typeof connectionSchema>;
export const agentStatuses: Record<string, string> = { queued: '等待执行', running: '正在执行', waiting_confirmation: '等待生成确认', waiting_connection: '等待本人Codex连接', conflict: '项目有新编辑，请核对', stopping: '正在停止后续操作', stopped: '已停止', completed: '已完成', failed: '执行失败', submission_unknown: '请求状态待核实', pending: '尚未执行', ready: '结果已保存', online: '已连接', offline: '已离线', revoked: '授权已撤销', expired: '授权已到期' };
export const agentToolTitles: Record<string, string> = { read_project: '读取项目', retrieve_context: '读取剧情与来源', read_artifact: '读取专业结构', write_script: '保存文字修订', write_artifact: '保存专业创作产物', patch_artifact: '修改专业字段', apply_canvas_ops: '编辑画布', submit_generation: '准备生成任务', inspect_generation: '查看生成进度', adopt_asset: '采用素材', validate_scene: '检查场景', validate_artifacts: '核对已保存产物', write_memory: '保存项目记忆', split_image: '图片切片', join_videos: '视频拼接' };

export async function getAgentRoles(projectId: string, workspace?: 'project' | 'scene') { return z.object({ roles: z.array(roleSchema) }).passthrough().parse(await api(`/projects/${projectId}/agent-roles${workspace ? `?workspace=${workspace}` : ''}`)).roles; }
export async function getAgentRuns(projectId: string) { return z.object({ runs: z.array(runSchema) }).parse(await api(`/projects/${projectId}/agent-runs`)).runs; }
export async function createAgentRun(projectId: string, input: { sceneId: string; workspace?: 'project' | 'scene'; branchId?: string; controllerNodeId: string; role: string; executor: string; deliveryMode: 'professional' | 'text_edit' | 'tool_action'; goal: string; targetIds: string[]; expectedRevision: number }) {
  return z.object({ run: runSchema }).passthrough().parse(await api(`/projects/${projectId}/agent-runs`, { method: 'POST', body: JSON.stringify({ ...input, operationId: operationId(), limits: { maxSteps: input.workspace === 'project' ? 24 : 12, maxTokens: 32768, maxCostMicros: 10000000 } }) })).run;
}
export async function controlAgentRun(projectId: string, runId: string, action: 'stop' | 'resume', expectedRevision: number) {
  return z.object({ run: runSchema }).passthrough().parse(await api(`/projects/${projectId}/agent-runs/${runId}/${action}`, { method: 'POST', body: JSON.stringify({ operationId: operationId(), ...(action === 'resume' ? { expectedRevision } : {}) }) })).run;
}
export async function retryUnknownAgentRun(projectId: string, runId: string, callId: string, expectedRevision: number, operation: string, maxSteps = 40) {
  return z.object({ run: runSchema }).passthrough().parse(await api(`/projects/${projectId}/agent-runs/${runId}/retry-unknown`, { method: 'POST', body: JSON.stringify({ operationId: operation, callId, expectedRevision, confirmed: true, maxSteps }) })).run;
}
export async function extendAgentRunLimit(projectId: string, runId: string, expectedRevision: number, operation: string, maxSteps: number) {
  return z.object({ run: agentRunSchema }).parse(await api(`/projects/${projectId}/agent-runs/${runId}/extend-limit`, { method: 'POST', body: JSON.stringify({ operationId: operation, expectedRevision, confirmed: true, maxSteps }) })).run;
}
export async function getProjectMemories(projectId: string) { return z.object({ memories: z.array(memorySchema) }).passthrough().parse(await api(`/projects/${projectId}/memories`)).memories; }
export async function readMemorySource(projectId: string, source: z.infer<typeof sourceSchema>) {
  return z.object({ source: sourceSchema.extend({ current: z.boolean() }).passthrough(), content: z.unknown() }).parse(await api(`/projects/${projectId}/context/source`, { method: 'POST', body: JSON.stringify({ kind: source.kind, id: source.id, revision: source.revision, ...(source.sceneId ? { sceneId: source.sceneId } : {}) }) }));
}
export async function createProjectMemory(projectId: string, input: { sceneId?: string; branchId?: string; kind: string; content: string; expectedRevision: number; sources: { kind: string; id: string; revision: number; sceneId?: string }[] }) {
  return z.object({ memory: memorySchema, revision: z.number() }).passthrough().parse(await api(`/projects/${projectId}/memories`, { method: 'POST', body: JSON.stringify({ ...input, operationId: operationId() }) }));
}
export async function getCodexConnections(projectId: string) { return z.object({ connections: z.array(connectionSchema) }).parse(await api(`/projects/${projectId}/connections`)).connections; }
export async function createCodexPairing(projectId: string, tools: string[]) {
  return z.object({ pairingId: z.string(), pairingCode: z.string(), projectId: z.string(), tools: z.array(z.string()), expiresAt: z.string(), connectionExpiresAt: z.string() }).parse(await api(`/projects/${projectId}/connections/pairings`, { method: 'POST', body: JSON.stringify({ operationId: operationId(), tools, expiresInSeconds: 3600 }) }));
}
export async function revokeCodexConnection(projectId: string, id: string) { return api(`/projects/${projectId}/connections/${id}/revoke`, { method: 'POST', body: JSON.stringify({ operationId: operationId() }) }); }

export function agentErrorMessage(value: unknown): string {
  if (typeof value === 'string') return value;
  const result = z.object({ message: z.string() }).safeParse(value);
  return result.success ? result.data.message : '';
}
