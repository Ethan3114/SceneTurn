import { z } from 'zod';

const DIRECT_THRESHOLD = 16 * 1024 * 1024;
const SINGLE_UPLOAD_LIMIT = 200_000_000;
const preparationSchema = z.discriminatedUnion('mode', [
  z.object({ mode: z.literal('multipart') }),
  z.object({ mode: z.literal('existing'), id: z.string(), result: z.unknown() }),
  z.object({ mode: z.literal('uploaded'), id: z.string() }),
  z.object({ mode: z.literal('direct'), id: z.string(), upload: z.object({
    fullSignedURL: z.url(), token: z.string().min(1), path: z.string().min(1), bucket: z.string().min(1), environmentId: z.string().min(1),
  }) }),
]);

type Request = (url: string, options?: RequestInit) => Promise<unknown>;
type SignedUpload = Extract<z.infer<typeof preparationSchema>, { mode: 'direct' }>['upload'];

export async function hashUploadFile(file: Blob): Promise<string> {
  const { sha256 } = await import('@noble/hashes/sha2.js');
  const digest = sha256.create();
  const reader = file.stream().getReader();
  try {
    for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) digest.update(chunk.value);
  } finally {
    reader.releaseLock();
  }
  return Array.from(digest.digest(), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function sendSignedUpload(upload: SignedUpload, file: File, contentType: string) {
  const url = new URL(upload.fullSignedURL);
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('私有存储上传地址无效。');
  if (file.size > SINGLE_UPLOAD_LIMIT) {
    const { default: cloudbase } = await import('@cloudbase/js-sdk');
    const storage = cloudbase.init({ env: upload.environmentId }).storage.from(upload.bucket);
    const { error } = await storage.uploadResumable(upload.path, file, { token: upload.token, contentType, upsert: false });
    if (error) throw new Error('大文件未完整上传。请重新选择同一文件继续。');
    return;
  }
  const response = await fetch(url, { method: 'PUT', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', headers: { 'Content-Type': contentType }, body: file });
  if (!response.ok) throw new Error('私有存储未接受上传。请重新选择同一文件继续。');
}

export async function directUpload(request: Request, path: string, file: File, operationFor: (sha256: string) => string) {
  if (file.size < DIRECT_THRESHOLD) return null;
  const sha256 = await hashUploadFile(file);
  const contentType = file.type || 'application/octet-stream';
  const preparation = preparationSchema.parse(await request(path, {
    method: 'POST', body: JSON.stringify({ operationId: operationFor(sha256), filename: file.name, contentType, bytes: file.size, sha256 }),
  }));
  if (preparation.mode === 'multipart') return null;

  const statusPath = `${path}/${encodeURIComponent(preparation.id)}`;
  if (preparation.mode === 'existing') return { statusPath, result: preparation.result };
  if (preparation.mode === 'direct') await sendSignedUpload(preparation.upload, file, contentType);
  return { statusPath, result: await request(`${statusPath}/complete`, { method: 'POST' }) };
}
