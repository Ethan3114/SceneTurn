import { createHash } from "node:crypto";
import { mkdir, open, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { BridgeHttpError, ProjectApi, validateApiBase } from "./api.js";
import { imageBytes, imageItemSchema, IMAGE_BYTES_LIMIT } from "./image-files.js";
import { CodexProtocol } from "./protocol.js";
import { productRoot, readPrivateJson, stateDirectory, writePrivateJson, type Credentials } from "./storage.js";


const referenceSchema = z.object({ assetId: z.uuid(), name: z.string(), sha256: z.string().regex(/^[a-f0-9]{64}$/) });
const jobSchema = z.object({ id: z.uuid(), projectId: z.uuid(), prompt: z.string(), transparentBackground: z.boolean(),
  referenceAssets: z.array(referenceSchema).max(5), threadId: z.string().nullable(), turnId: z.string().nullable(),
  stopRequested: z.boolean(), status: z.string() }).passthrough();
const claimSchema = z.object({ result: z.object({ job: jobSchema, submissionAttempted: z.boolean(), imageCompleted: z.boolean(), itemId: z.string().nullable() }).nullable() });
const imageCheckpointSchema = z.object({ jobId: z.uuid(), threadId: z.string().nullable(), turnId: z.string().nullable(),
  turnStartAttempted: z.boolean(), image: imageItemSchema.nullable(), imported: z.boolean() }).strict();
const notification = z.object({ threadId: z.string(), turnId: z.string(), item: z.record(z.string(), z.unknown()) });
const turnSchema = z.object({ id: z.string(), status: z.string() }).passthrough();

async function checkedResponse(response: Response): Promise<unknown> {
  const value: unknown = await response.json();
  if (!response.ok) {
    const error = z.object({ error: z.object({ code: z.string() }) }).parse(value);
    throw new BridgeHttpError(error.error.code, response.status);
  }
  return value;
}

async function stageReference(credentials: Credentials, jobId: string, reference: z.infer<typeof referenceSchema>, directory: string): Promise<string> {
  const response = await fetch(`${validateApiBase(credentials.apiBase)}/connections/images/${jobId}/references/${reference.assetId}/content`, {
    headers: { Authorization: `Bearer ${credentials.token}` }, redirect: "error", signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) { await checkedResponse(response); throw new Error("CODEX_IMAGE_REFERENCE_FAILED"); }
  if (!response.body) throw new Error("CODEX_IMAGE_REFERENCE_EMPTY");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      total += chunk.value.length;
      if (total > IMAGE_BYTES_LIMIT) throw new Error("CODEX_IMAGE_REFERENCE_TOO_LARGE");
      chunks.push(chunk.value);
    }
  } finally { await reader.cancel(); }
  const bytes = Buffer.concat(chunks);
  if (createHash("sha256").update(bytes).digest("hex") !== reference.sha256) throw new Error("CODEX_IMAGE_REFERENCE_CHANGED");
  const suffix = new Map([["image/png", ".png"], ["image/jpeg", ".jpg"], ["image/webp", ".webp"], ["image/gif", ".gif"]]).get(response.headers.get("content-type")?.split(";")[0] ?? "");
  if (!suffix) throw new Error("CODEX_IMAGE_REFERENCE_TYPE_INVALID");
  const path = resolve(directory, `${reference.sha256}${suffix}`);
  const previous = await readFile(path).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; return null; });
  if (previous) {
    if (createHash("sha256").update(previous).digest("hex") !== reference.sha256) throw new Error("CODEX_IMAGE_REFERENCE_CHANGED");
  } else {
    const output = await open(path, "wx", 0o600);
    try { await output.writeFile(bytes); await output.sync(); } finally { await output.close(); }
  }
  return path;
}

export async function imageAccountAvailable(): Promise<boolean> {
  const protocol = new CodexProtocol(stateDirectory);
  try {
    await protocol.initialize();
    const account = z.object({ account: z.object({ type: z.string(), planType: z.string().optional() }).passthrough().nullable() }).parse(await protocol.request("account/read", { refreshToken: false }));
    if (account.account?.type !== "chatgpt" || !account.account.planType || account.account.planType === "free") return false;
    const config = z.object({ config: z.object({ model: z.string().nullable().optional(), model_provider: z.string().nullable().optional() }).passthrough() }).parse(await protocol.client.request("config/read", { includeLayers: false }));
    if (config.config.model_provider && config.config.model_provider !== "openai") return false;
    const models = z.object({ data: z.array(z.object({ id: z.string(), model: z.string(), isDefault: z.boolean(), inputModalities: z.array(z.string()) }).passthrough()) }).parse(await protocol.request("model/list", { includeHidden: true }));
    const selected = models.data.find((item) => config.config.model ? item.model === config.config.model || item.id === config.config.model : item.isDefault);
    return selected?.inputModalities.includes("image") === true;
  } finally { await protocol.close(); }
}

export async function runNextImageJob(credentials: Credentials): Promise<boolean> {
  const api = new ProjectApi(credentials);
  const claim = claimSchema.parse(await api.post("/connections/images/claim")).result;
  if (!claim) return false;
  const job = claim.job;
  if (job.projectId !== credentials.projectId) throw new Error("PROJECT_SCOPE_MISMATCH");
  const filename = `image-${job.id}.json`;
  const previous = await readPrivateJson(resolve(stateDirectory, filename)).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
    return null;
  });
  const checkpoint = previous ? imageCheckpointSchema.parse(previous) : { jobId: job.id, threadId: job.threadId, turnId: job.turnId,
    turnStartAttempted: claim.submissionAttempted, image: null, imported: false };
  if (checkpoint.jobId !== job.id || checkpoint.threadId && job.threadId && checkpoint.threadId !== job.threadId) throw new Error("CODEX_IMAGE_CHECKPOINT_MISMATCH");
  checkpoint.turnStartAttempted ||= claim.submissionAttempted;
  checkpoint.threadId ??= job.threadId;
  checkpoint.turnId ??= job.turnId;
  const save = () => writePrivateJson(filename, checkpoint);
  await save();
  const cwd = resolve(productRoot, ".intermediate/codex-image-workspace", job.projectId, job.id);
  const inputsPath = resolve(cwd, "inputs");
  await mkdir(inputsPath, { recursive: true, mode: 0o700 });
  const references: string[] = [];
  for (const reference of job.referenceAssets) references.push(await stageReference(credentials, job.id, reference, inputsPath));
  const protocol = new CodexProtocol(cwd, { inputsPath });
  let finished = false;
  let reject: (error: Error) => void = () => undefined;
  let succeed: () => void = () => undefined;
  const complete = new Promise<void>((done, failed) => { succeed = done; reject = failed; });
  void complete.catch(() => undefined);
  let pending = Promise.resolve();
  const event = (eventType: string, extra: Record<string, unknown> = {}) => api.post(`/connections/images/${job.id}/events`, {
    eventType, threadId: checkpoint.threadId, turnId: checkpoint.turnId, ...extra,
  });
  const importImage = async () => {
    if (!checkpoint.image || !checkpoint.threadId || !checkpoint.turnId) throw new Error("CODEX_IMAGE_RESULT_MISSING");
    const expected = job.referenceAssets.map((item) => item.sha256).sort();
    const actual = (checkpoint.image.inputImages ?? []).map((item) => item.sha256).sort();
    if (JSON.stringify(expected) !== JSON.stringify(actual)) throw new Error("CODEX_IMAGE_REFERENCE_MISMATCH");
    await event("image_completed", { itemId: checkpoint.image.id, inputImages: checkpoint.image.inputImages ?? [] });
    const data = await imageBytes(checkpoint.image, checkpoint.threadId);
    const body = new FormData();
    body.set("metadata", JSON.stringify({ threadId: checkpoint.threadId, turnId: checkpoint.turnId, itemId: checkpoint.image.id, sha256: data.sha256, source: data.source }));
    body.set("file", new Blob([new Uint8Array(data.bytes)], { type: "image/png" }), "codex-generated.png");
    await checkedResponse(await fetch(`${validateApiBase(credentials.apiBase)}/connections/images/${job.id}/result`, {
      method: "POST", headers: { Authorization: `Bearer ${credentials.token}` }, body, redirect: "error", signal: AbortSignal.timeout(90000),
    }));
    checkpoint.imported = true;
    await save();
  };
  const inspectItem = async (raw: unknown, turnId: string) => {
    const parsed = imageItemSchema.safeParse(raw);
    if (!parsed.success) return;
    const item = parsed.data;
    if (checkpoint.image && checkpoint.image.id !== item.id || claim.itemId && claim.itemId !== item.id) throw new Error("CODEX_IMAGE_MULTIPLE_CALLS");
    checkpoint.turnId = turnId;
    if (item.status === "failed") throw new Error("CODEX_IMAGE_NATIVE_FAILED");
    if (item.status === "completed") {
      checkpoint.image = item;
      await save();
      if (!checkpoint.imported) await importImage();
    } else {
      await event("image_started", { itemId: item.id });
    }
  };
  protocol.on("notification", (method: string, value: unknown) => {
    pending = pending.then(async () => {
      if (method === "item/started" || method === "item/completed") {
        const data = notification.parse(value);
        if (data.threadId !== checkpoint.threadId) return;
        if (checkpoint.turnId && checkpoint.turnId !== data.turnId) throw new Error("CODEX_IMAGE_TURN_MISMATCH");
        await inspectItem(data.item, data.turnId);
      }
      if (method === "turn/completed") {
        const data = z.object({ threadId: z.string(), turn: turnSchema }).parse(value);
        if (data.threadId !== checkpoint.threadId) return;
        checkpoint.turnId = data.turn.id;
        await event("turn_completed");
        if (!checkpoint.imported) throw new Error("CODEX_IMAGE_RESULT_MISSING");
        finished = true;
        succeed();
      }
      if (method === "thread/tokenUsage/updated") {
        const data = z.object({ threadId: z.string(), tokenUsage: z.record(z.string(), z.unknown()) }).parse(value);
        if (data.threadId === checkpoint.threadId) await event("heartbeat", { usage: data.tokenUsage });
      }
    }).catch((error: unknown) => reject(error instanceof Error ? error : new Error("CODEX_IMAGE_EVENT_INVALID")));
  });
  protocol.on("closed", () => { if (!finished) reject(new Error("CODEX_IMAGE_CONNECTION_CLOSED")); });
  const shutdown = () => reject(new Error("CODEX_IMAGE_STOPPED"));
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  const heartbeat = setInterval(() => {
    void event("heartbeat").then((value) => {
      if (z.object({ job: jobSchema }).parse(value).job.stopRequested) reject(new Error("CODEX_IMAGE_STOPPED"));
    }).catch((error: unknown) => reject(error instanceof Error ? error : new Error("CODEX_IMAGE_AUTH_LOST")));
  }, 5000);
  const deadline = setTimeout(() => reject(new Error("CODEX_IMAGE_TIMEOUT")), 10 * 60 * 1000);
  try {
    await protocol.initialize();
    await protocol.assertChatgptAccount();
    await protocol.assertRestrictedConfiguration();
    const resumeThread = checkpoint.turnStartAttempted && checkpoint.threadId;
    const thread = z.object({ thread: z.object({ id: z.string() }), activePermissionProfile: z.object({ id: z.literal("openfmv_image") }) }).passthrough().parse(await protocol.request(resumeThread ? "thread/resume" : "thread/start", {
      cwd, permissions: "openfmv_image", runtimeWorkspaceRoots: [cwd], approvalPolicy: "never",
      ...(resumeThread ? { threadId: checkpoint.threadId, excludeTurns: true } : { dynamicTools: [], serviceName: "openfmv-image-job",
        developerInstructions: "仅使用原生image_gen.imagegen完成本人已确认的这一项图片任务。仅使用给定参考图片，保持图片要求。只调用一次图片生成；不使用命令、文件编辑、其他工具或其他服务。" }),
    }));
    checkpoint.threadId = thread.thread.id;
    await save();
    await protocol.assertMcpIsolated(checkpoint.threadId);
    await event("thread_started");
    if (checkpoint.image && !checkpoint.imported) await importImage();
    if (checkpoint.turnStartAttempted) {
      const turns = z.object({ data: z.array(turnSchema) }).parse(await protocol.request("thread/turns/list", { threadId: checkpoint.threadId, sortDirection: "desc", limit: 100 }));
      const onlyTurn = turns.data[0];
      if (!checkpoint.turnId && turns.data.length === 1 && onlyTurn) checkpoint.turnId = onlyTurn.id;
      const turn = turns.data.find((item) => item.id === checkpoint.turnId);
      if (!turn) throw new Error("CODEX_IMAGE_SUBMISSION_UNKNOWN");
      await save();
      const items = z.object({ data: z.array(z.object({ turnId: z.string(), item: z.unknown() })), nextCursor: z.string().nullable().optional() }).parse(await protocol.request("thread/items/list", { threadId: checkpoint.threadId, turnId: turn.id, limit: 100 }));
      if (items.nextCursor) throw new Error("CODEX_IMAGE_ITEM_LIMIT");
      for (const entry of items.data) {
        if (entry.turnId !== turn.id) throw new Error("CODEX_IMAGE_TURN_MISMATCH");
        await inspectItem(entry.item, turn.id);
      }
      if (turn.status !== "inProgress") {
        await event("turn_completed");
        if (!checkpoint.imported) throw new Error("CODEX_IMAGE_RESULT_MISSING");
        finished = true;
        succeed();
      }
    } else {
      await event("turn_requested");
      checkpoint.turnStartAttempted = true;
      await save();
      const requirements = { prompt: job.prompt, transparent_background: job.transparentBackground,
        ...(references.length ? { referenced_image_paths: references } : {}) };
      const turn = z.object({ turn: turnSchema }).parse(await protocol.request("turn/start", { threadId: checkpoint.threadId, effort: "medium",
        input: [{ type: "text", text: "请调用一次原生image_gen.imagegen，参数如下；生成完成后结束。\n" + JSON.stringify(requirements), text_elements: [] }] }));
      checkpoint.turnId = turn.turn.id;
      await save();
      await event("turn_started");
    }
    await complete;
    return true;
  } catch (error) {
    if (checkpoint.threadId && checkpoint.turnId) await protocol.request("turn/interrupt", { threadId: checkpoint.threadId, turnId: checkpoint.turnId }).catch(() => undefined);
    const code = error instanceof Error && /^[A-Z_]+$/.test(error.message) ? error.message : "CODEX_IMAGE_CONNECTION_FAILED";
    await event("disconnected", { errorCode: code }).catch(() => undefined);
    await api.post("/connections/heartbeat", { capabilities: [] }).catch(() => undefined);
    throw error;
  } finally {
    process.removeListener("SIGINT", shutdown);
    process.removeListener("SIGTERM", shutdown);
    clearInterval(heartbeat);
    clearTimeout(deadline);
    await protocol.close();
  }
}
