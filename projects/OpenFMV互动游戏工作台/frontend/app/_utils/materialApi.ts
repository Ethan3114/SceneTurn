import { z } from 'zod';
import { api, operationId } from './cloudApi';
import { agentRunSchema } from './agentApi';

const summary = z.object({ id: z.string(), projectId: z.string(), sceneId: z.string(), title: z.string(), createdAt: z.string(), updatedAt: z.string() });
const imageJob = z.object({ id: z.string(), label: z.string(), frameKey: z.string().nullable(), status: z.string(), assetId: z.string().nullable(), width: z.number(), height: z.number(), errorCode: z.string().nullable() });
const delivery = z.object({ kind: z.string(), text: z.string(), images: z.array(imageJob), segments: z.array(z.object({ key: z.string(), firstFrameKey: z.string(), lastFrameKey: z.string(), durationSeconds: z.number() })) });
const node = z.object({ id: z.string(), type: z.string(), title: z.string().optional(), contentHash: z.string(), producerRole: z.string().nullable(), metadata: z.record(z.string(), z.unknown()).optional(), delivery: delivery.nullable().optional() }).passthrough();
const review = z.object({ id: z.string(), nodeId: z.string(), status: z.string(), questions: z.array(z.object({ id: z.string(), question: z.string(), suggestedAnswer: z.string() })), remainingQuestions: z.number(), imageCount: z.number(), images: z.array(z.object({ label: z.string(), width: z.number(), height: z.number() })), canConfirm: z.boolean(), message: z.string() });
const reference = z.object({ nodeId: z.string(), title: z.string(), contentHash: z.string(), revision: z.number() });
const referenceAsset = z.object({ assetId: z.string(), name: z.string(), sha256: z.string() });
const conversation = summary.extend({ revision: z.number(), nodes: z.array(node), selectedReferenceAssets: z.array(referenceAsset), pendingReview: review.nullable().optional(), capabilities: z.object({ text: z.boolean(), image: z.boolean(), video: z.boolean(), textModel: z.string(), videoModel: z.string() }),
  messages: z.array(z.object({ id: z.string(), content: z.string(), references: z.array(reference), run: agentRunSchema, createdAt: z.string() })) });
export type MaterialConversation = z.infer<typeof conversation>;
export type MaterialNode = z.infer<typeof node>;
export type MaterialReference = z.infer<typeof reference>;
export type MaterialReferenceAsset = z.infer<typeof referenceAsset>;
const path = (projectId: string, sceneId: string) => `/projects/${encodeURIComponent(projectId)}/scenes/${encodeURIComponent(sceneId)}/material-conversations`;
export const listMaterialConversations = async (projectId: string, sceneId: string) => z.object({ conversations: z.array(summary) }).parse(await api(path(projectId, sceneId))).conversations;
export const createMaterialConversation = async (projectId: string, sceneId: string) => z.object({ conversation }).parse(await api(path(projectId, sceneId), { method: 'POST', body: JSON.stringify({ operationId: operationId() }) })).conversation;
export const getMaterialConversation = async (projectId: string, sceneId: string, id: string) => z.object({ conversation }).parse(await api(`${path(projectId, sceneId)}/${id}`)).conversation;
export const sendMaterialMessage = async (value: MaterialConversation, content: string, references: MaterialReference[], expectedRevision: number, id: string, referenceAssetIds: string[] = []) => z.object({ conversation }).parse(await api(`${path(value.projectId, value.sceneId)}/${value.id}/messages`, { method: 'POST', body: JSON.stringify({ operationId: id, content, references: references.map(({ nodeId, contentHash }) => ({ nodeId, contentHash })), referenceAssetIds, expectedRevision }) })).conversation;
export const answerMaterialReview = async (value: MaterialConversation, reviewId: string, answers: Record<string, string>, requestedChanges: string, expectedRevision: number, id: string, referenceAssetIds: string[] = []) => z.object({ conversation }).parse(await api(`${path(value.projectId, value.sceneId)}/${value.id}/answers`, { method: 'POST', body: JSON.stringify({ operationId: id, reviewId, answers, requestedChanges, referenceAssetIds, expectedRevision }) })).conversation;
