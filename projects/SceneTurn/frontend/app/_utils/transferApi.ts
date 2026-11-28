import { z } from 'zod';
import { api, operationId } from './cloudApi';
import { directUpload } from './directUpload';

const transferSchema = z.object({
  id: z.string(), kind: z.enum(['import', 'backup', 'game']), status: z.enum(['queued', 'processing', 'awaiting_confirmation', 'blocked', 'ready', 'failed']),
  projectId: z.string().nullable(), sourceRevision: z.number().nullable(), sourceFormat: z.string().nullable(),
  projects: z.array(z.object({ id: z.string(), title: z.string(), sceneCount: z.number(), canvasNodeCount: z.number(), assetCount: z.number() })),
  mappings: z.array(z.object({ kind: z.enum(['project', 'scene', 'node', 'asset']), sourceId: z.string(), targetId: z.string() })),
  issues: z.array(z.object({ code: z.string(), message: z.string(), sourceId: z.string().nullable().optional() })),
  confirmationHash: z.string().nullable(), downloadUrl: z.string().nullable(), previewUrl: z.string().nullable(),
  errorCode: z.string().nullable(), errorMessage: z.string().nullable(), createdAt: z.string(), updatedAt: z.string(),
});
export type ProjectTransfer = z.infer<typeof transferSchema>;
export async function getTransfer(id: string) {
  return z.object({ transfer: transferSchema }).parse(await api(`/transfers/${encodeURIComponent(id)}`)).transfer;
}
export async function listTransfers(projectId?: string) {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  return z.object({ transfers: z.array(transferSchema) }).parse(await api(`/transfers${query}`)).transfers;
}
export async function prepareExport(projectId: string, revision: number, kind: 'backup' | 'game', operation = operationId()) {
  return z.object({ transfer: transferSchema }).parse(await api(`/projects/${encodeURIComponent(projectId)}/exports`, {
    method: 'POST', body: JSON.stringify({ kind, expectedRevision: revision, operationId: operation }),
  })).transfer;
}
export async function uploadProjectArchive(file: File, operation = operationId()) {
  const staged = await directUpload(api, '/transfers/imports/direct-upload', file, () => operation);
  if (staged) return z.object({ transfer: transferSchema }).parse(staged.result).transfer;
  const body = new FormData(); body.append('file', file); body.append('operationId', operation);
  return z.object({ transfer: transferSchema }).parse(await api('/transfers/imports', { method: 'POST', body })).transfer;
}
export async function confirmTransfer(transfer: ProjectTransfer) {
  if (!transfer.confirmationHash) throw new Error('请等待完整检查并重新核对确认内容');
  return z.object({ transfer: transferSchema }).parse(await api(`/transfers/${encodeURIComponent(transfer.id)}/confirm`, {
    method: 'POST', body: JSON.stringify({ confirmationHash: transfer.confirmationHash }),
  })).transfer;
}
export async function retryTransfer(transfer: ProjectTransfer) {
  return z.object({ transfer: transferSchema }).parse(await api(`/transfers/${encodeURIComponent(transfer.id)}/retry`, { method: 'POST' })).transfer;
}
