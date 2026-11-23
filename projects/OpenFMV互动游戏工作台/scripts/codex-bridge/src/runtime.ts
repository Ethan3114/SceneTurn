import { createHash } from "node:crypto";
import { cp, mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { BridgeHttpError, ProjectApi } from "./api.js";
import { CodexProtocol, type DynamicCall } from "./protocol.js";
import { imageAccountAvailable, runNextImageJob } from "./image-runtime.js";
import { checkpointSchema, productRoot, stateDirectory, writePrivateJson, type Checkpoint, type Credentials } from "./storage.js";

const toolSchema = z.object({ name: z.string(), description: z.string(), inputSchema: z.unknown() }).passthrough();
const runSchema = z.object({
  id: z.string(), projectId: z.string(), projectRevision: z.number().int().positive(),
  threadId: z.string().nullable().optional(), turnId: z.string().nullable().optional(),
  result: z.record(z.string(), z.unknown()).nullable().optional(),
  toolCatalogVersion: z.enum(["R1", "R2", "R3", "R4"]).optional(),
}).passthrough();
const claimSchema = z.object({ result: z.object({ run: runSchema, instructions: z.string(), tools: z.array(toolSchema), skills: z.array(z.string()).optional(), usageBaseline: z.record(z.string(), z.number().int().nonnegative()).nullable().optional() }).nullable() });
const threadResponse = z.object({ thread: z.object({ id: z.string() }).passthrough(), model: z.string() }).passthrough();
const turnSchema = z.object({ id: z.string(), status: z.string(), items: z.array(z.record(z.string(), z.unknown())).optional() }).passthrough();
const turnResponse = z.object({ turn: turnSchema }).passthrough();
const completedSchema = z.object({ threadId: z.string(), turn: turnSchema });
const tokenSchema = z.object({ threadId: z.string(), turnId: z.string(), tokenUsage: z.record(z.string(), z.unknown()) });
const heartbeatSchema = z.object({ activeRuns: z.array(z.object({ id: z.string(), stopRequested: z.boolean() }).passthrough()) }).passthrough();
const argumentSchema = z.record(z.string(), z.unknown());

const correctableTools = new Map<string, ReadonlySet<string>>([
  ["apply_story_ops", new Set()],
  ["prepare_scene_media", new Set()],
  ["edit_interaction", new Set(["INTERACTION_TIME_INVALID", "INTERACTION_TARGET_INVALID"])],
  ["edit_timeline", new Set(["MEDIA_DURATION_CONFLICT", "ASSET_TYPE_UNSUPPORTED"])],
  ["arrange_story", new Set(["STORY_CONNECTION_INVALID"])],
  ["place_interaction", new Set(["BUTTON_RECT_INVALID"])],
  ["move_timeline_clip", new Set(["TRACK_TYPE_MISMATCH"])],
]);
const correctionMessages: Readonly<Record<string, string>> = {
  AGENT_TOOL_ARGUMENTS_INVALID: "参数没有通过工具结构校验。请按照当前工具的Schema修正参数。",
  INTERACTION_TIME_INVALID: "互动开始时间加持续时间必须位于场景时间线内。请先读取当前场景时间线，再调整互动范围。",
  INTERACTION_TARGET_INVALID: "互动需要有效的其他场景或结尾目标。请先读取当前故事节点，再修正跳转目标。",
  MEDIA_DURATION_CONFLICT: "裁切范围超过媒体真实时长。请先读取当前媒体时长和播放速度，再修正裁切范围。",
  ASSET_TYPE_UNSUPPORTED: "时间线只接受图片、视频和音频。请读取当前素材并选择支持的媒体类型。",
  STORY_CONNECTION_INVALID: "剧情连接需要有效的起点和终点。请读取当前故事节点，再修正连接。",
  BUTTON_RECT_INVALID: "互动位置和尺寸必须完整位于画面内。请修正归一化坐标及宽高。",
  TRACK_TYPE_MISMATCH: "目标轨道需要与原片段类型相同。请读取当前时间线，再选择同类型轨道。",
};

export function correctableToolFailure(toolName: string, error: unknown) {
  const allowed = correctableTools.get(toolName);
  if (!(error instanceof BridgeHttpError) || error.status !== 422 || !allowed
    || (error.code !== "AGENT_TOOL_ARGUMENTS_INVALID" && !allowed.has(error.code))) return null;
  return {
    error: { code: error.code, message: correctionMessages[error.code] },
    executed: false,
    correctionRequired: true,
    instruction: "本次操作未写入项目。请核对当前状态并修正参数，再通过新的工具调用提交。",
  };
}

function finalText(turn: z.infer<typeof turnSchema>): string {
  return (turn.items ?? []).filter((item) => item.type === "agentMessage" && typeof item.text === "string").map((item) => item.text as string).join("\n");
}

export async function runBridge(credentials: Credentials, once: boolean): Promise<void> {
  const api = new ProjectApi(credentials);
  let ending = false;
  let currentProtocol: CodexProtocol | undefined;
  let checkpoint: Checkpoint | undefined;
  let usage: Record<string, unknown> | null = null;
  let activeTask: Promise<void> | undefined;
  const shutdown = () => { ending = true; void currentProtocol?.close(); };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  try {
    const imagesEnabled = await imageAccountAvailable();
    await api.post("/connections/heartbeat", { capabilities: imagesEnabled ? ["native_image_v1"] : [] });
    while (!ending) {
      await api.post("/connections/heartbeat");
      if (imagesEnabled && await runNextImageJob(credentials)) {
        if (once) return;
        continue;
      }
      const claim = claimSchema.parse(await api.post("/connections/claim")).result;
      if (claim === null) {
        if (once) return;
        await new Promise((done) => setTimeout(done, 2000));
        continue;
      }
      if (claim.run.projectId !== credentials.projectId) throw new Error("PROJECT_SCOPE_MISMATCH");
      const allowed = new Set(claim.tools.map((tool) => tool.name));
      const checkpointPath = resolve(stateDirectory, `${claim.run.id}.json`);
      const existing = await readFile(checkpointPath, "utf8").then((text) => checkpointSchema.parse(JSON.parse(text) as unknown), (error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
        return undefined;
      });
      const catalogVersion = claim.run.toolCatalogVersion ?? "R1";
      if (existing && (existing.toolCatalogVersion ?? "R1") !== catalogVersion) throw new Error("CODEX_TOOL_CATALOG_MISMATCH");
      checkpoint = existing ?? { runId: claim.run.id, threadId: claim.run.threadId ?? null, turnId: claim.run.turnId ?? null,
        revision: claim.run.projectRevision, usage: null, pendingCallId: null, codexResumeOperationId: null,
        threadUsageBaseline: claim.usageBaseline ?? null, threadUsageTotal: claim.usageBaseline ?? null };
      checkpoint.toolCatalogVersion = catalogVersion;
      const resumeOperationId = typeof claim.run.result?.codexResumeOperationId === "string" ? claim.run.result.codexResumeOperationId : null;
      if (resumeOperationId !== checkpoint.codexResumeOperationId) {
        checkpoint.turnId = claim.run.turnId ?? null;
        checkpoint.pendingCallId = null;
        checkpoint.threadUsageBaseline = checkpoint.threadUsageTotal;
        checkpoint.usage = null;
        checkpoint.codexResumeOperationId = resumeOperationId;
      }
      checkpoint.revision = claim.run.projectRevision;
      usage = checkpoint.usage;
      await writePrivateJson(`${claim.run.id}.json`, checkpoint);
      const cwd = resolve(productRoot, ".intermediate/codex-workspace", credentials.projectId);
      await mkdir(cwd, { recursive: true, mode: 0o700 });
      const bundledSkills = new Set(["openfmv-story", "openfmv-layout", "openfmv-timeline", "openfmv-interaction", "openfmv-check"]);
      const skillInputs = [];
      for (const name of claim.skills ?? []) {
        if (!bundledSkills.has(name)) throw new Error("SKILL_NOT_AUTHORIZED");
        const target = resolve(cwd, ".agents/skills", name);
        await cp(resolve(productRoot, "skills", name), target, { recursive: true, force: true });
        skillInputs.push({ type: "skill", name, path: resolve(target, "SKILL.md") });
      }
      const protocol = new CodexProtocol(cwd);
      currentProtocol = protocol;
      let settled = false;
      let model = "unknown";
      let finish: () => void = () => undefined;
      let reject: (error: Error) => void = () => undefined;
      const completion = new Promise<void>((resolve, rejectPromise) => { finish = resolve; reject = rejectPromise; });
      void completion.catch(() => undefined);
      const event = async (eventType: string, extra: Record<string, unknown> = {}) => {
        await api.post("/connections/events", { runId: claim.run.id, eventType, threadId: checkpoint?.threadId ?? null, turnId: checkpoint?.turnId ?? null, ...extra });
      };
      const complete = async (turn: z.infer<typeof turnSchema>) => {
        if (settled || !checkpoint) return;
        settled = true;
        await api.post("/connections/complete", { runId: claim.run.id, threadId: checkpoint.threadId, turnId: turn.id,
          result: { finalText: finalText(turn), status: turn.status }, usage });
        finish();
      };
      protocol.on("toolCall", (id: string | number, call: DynamicCall) => {
        const perform = async () => {
          if (!checkpoint || call.threadId !== checkpoint.threadId || (checkpoint.turnId && call.turnId !== checkpoint.turnId) || !allowed.has(call.tool)) throw new Error("TOOL_SCOPE_MISMATCH");
          const body = argumentSchema.parse(call.arguments);
          checkpoint.turnId = call.turnId;
          checkpoint.pendingCallId = call.callId;
          await writePrivateJson(`${claim.run.id}.json`, checkpoint);
          const operationId = createHash("sha256").update(`${claim.run.id}:${call.callId}`).digest("hex");
          let response;
          try {
            response = z.object({ result: z.record(z.string(), z.unknown()) }).parse(await api.post("/connections/tools", {
              runId: claim.run.id, operationId, expectedRevision: checkpoint.revision, toolName: call.tool, arguments: body,
            }));
          } catch (error) {
            const failure = correctableToolFailure(call.tool, error);
            if (!failure) throw error;
            checkpoint.pendingCallId = null;
            await writePrivateJson(`${claim.run.id}.json`, checkpoint);
            protocol.respond(id, { success: false, contentItems: [{ type: "inputText", text: JSON.stringify(failure) }] });
            return;
          }
          if (typeof response.result.revision === "number") checkpoint.revision = response.result.revision;
          checkpoint.pendingCallId = null;
          await writePrivateJson(`${claim.run.id}.json`, checkpoint);
          protocol.respond(id, { success: true, contentItems: [{ type: "inputText", text: JSON.stringify(response.result) }] });
        };
        activeTask = (activeTask ?? Promise.resolve()).then(perform).catch((error: unknown) => {
          protocol.respond(id, { success: false, contentItems: [{ type: "inputText", text: error instanceof Error ? error.message.replace(/[^A-Z0-9_]/g, "").slice(0, 100) || "TOOL_EXECUTION_FAILED" : "TOOL_EXECUTION_FAILED" }] });
          reject(error instanceof Error ? error : new Error("TOOL_EXECUTION_FAILED"));
        });
      });
      protocol.on("notification", (method: string, params: unknown) => {
        if (method === "thread/tokenUsage/updated") {
          const data = tokenSchema.parse(params);
          if (data.threadId !== checkpoint?.threadId) return;
          const counts = z.record(z.string(), z.number().int().nonnegative()).parse(data.tokenUsage.total);
          const current = Object.fromEntries(Object.entries(counts).map(([key, value]) => {
            const difference = value - (checkpoint?.threadUsageBaseline?.[key] ?? 0);
            if (difference < 0) throw new Error("CODEX_USAGE_COUNTER_CHANGED");
            return [key, difference];
          }));
          checkpoint.threadUsageTotal = counts;
          usage = { ...current, model, threadTotals: counts, baseline: checkpoint.threadUsageBaseline, rawTokenUsage: data.tokenUsage };
          checkpoint.usage = usage;
          void writePrivateJson(`${claim.run.id}.json`, checkpoint).then(() => event("usage", { usage })).catch(() => reject(new Error("USAGE_PERSIST_FAILED")));
        }
        if (method === "turn/completed") {
          const data = completedSchema.parse(params);
          if (data.threadId === checkpoint?.threadId) void (activeTask ?? Promise.resolve()).then(() => complete(data.turn)).catch(() => reject(new Error("COMPLETION_PERSIST_FAILED")));
        }
      });
      protocol.on("closed", () => { if (!settled) reject(new Error("CODEX_CONNECTION_CLOSED")); });
      const heartbeatTimer = setInterval(() => {
        void api.post("/connections/heartbeat").then((value) => {
          const status = heartbeatSchema.parse(value).activeRuns.find((run) => run.id === claim.run.id);
          if (!status || status.stopRequested) {
            if (checkpoint?.threadId && checkpoint.turnId) void protocol.request("turn/interrupt", { threadId: checkpoint.threadId, turnId: checkpoint.turnId }).catch(() => reject(new Error("CODEX_INTERRUPT_FAILED")));
            if (!status) reject(new Error("CONNECTION_LEASE_LOST"));
          }
        }).catch(() => { void protocol.close(); reject(new Error("CONNECTION_AUTH_LOST")); });
      }, 5000);
      const timeout = setTimeout(() => reject(new Error("CODEX_TURN_TIMEOUT")), 10 * 60 * 1000);
      try {
        await protocol.initialize();
        await protocol.assertRestrictedConfiguration();
        if (skillInputs.length) {
          const inventory = z.object({ data: z.array(z.object({ skills: z.array(z.object({ name: z.string(), enabled: z.boolean() }).passthrough()) }).passthrough()) }).parse(await protocol.request("skills/list", { cwds: [cwd], forceReload: true }));
          const enabled = new Set(inventory.data.flatMap((entry) => entry.skills.filter((skill) => skill.enabled).map((skill) => skill.name)));
          if (skillInputs.some((skill) => !enabled.has(skill.name))) throw new Error("PROJECT_SKILLS_UNAVAILABLE");
        }
        const dynamicTools = claim.tools.map((tool) => ({ type: "function", name: tool.name, description: tool.description, inputSchema: tool.inputSchema }));
        const settings = { cwd, sandbox: "read-only", approvalPolicy: "never" };
        const thread = threadResponse.parse(await protocol.request(checkpoint.threadId ? "thread/resume" : "thread/start", {
          ...settings, ...(checkpoint.threadId ? { threadId: checkpoint.threadId, excludeTurns: true } : { dynamicTools, serviceName: "openfmv-workbench", developerInstructions: "只使用OpenFMV项目工具完成本次授权任务。工具输入直接遵循所给Schema，本机连接绑定当前读取的项目版本和唯一操作标识。禁止其他文件、命令、网络和外部服务操作。" }),
        }));
        checkpoint.threadId = thread.thread.id;
        model = thread.model;
        await writePrivateJson(`${claim.run.id}.json`, checkpoint);
        await protocol.assertMcpIsolated(checkpoint.threadId);
        await event("thread_started");
        if (checkpoint.turnId) {
          const turns = z.object({ data: z.array(turnSchema) }).parse(await protocol.request("thread/turns/list", { threadId: checkpoint.threadId, sortDirection: "desc", limit: 100 }));
          const existingTurn = turns.data.find((turn) => turn.id === checkpoint?.turnId);
          if (!existingTurn) throw new Error("CODEX_PREVIOUS_TURN_NOT_FOUND");
          if (existingTurn.status !== "inProgress") await complete(existingTurn);
        } else {
          const authorized = heartbeatSchema.parse(await api.post("/connections/heartbeat")).activeRuns.find((run) => run.id === claim.run.id);
          if (!authorized) throw new Error("CONNECTION_LEASE_LOST");
          if (authorized.stopRequested) {
            await event("interrupted", { errorCode: "STOPPED_BEFORE_CODEX_TURN" });
            settled = true;
            finish();
          } else {
            checkpoint.requestedReasoningEffort = "medium";
            await writePrivateJson(`${claim.run.id}.json`, checkpoint);
            const turn = turnResponse.parse(await protocol.request("turn/start", { threadId: checkpoint.threadId, effort: checkpoint.requestedReasoningEffort, input: [{ type: "text", text: claim.instructions, text_elements: [] }, ...skillInputs] }));
            checkpoint.turnId = turn.turn.id;
            await writePrivateJson(`${claim.run.id}.json`, checkpoint);
            await event("turn_started");
          }
        }
        await completion;
      } catch (error) {
        if (checkpoint.threadId && checkpoint.turnId) await protocol.request("turn/interrupt", { threadId: checkpoint.threadId, turnId: checkpoint.turnId }).catch(() => undefined);
        await event("disconnected", { errorCode: error instanceof Error && /^[A-Z_]+$/.test(error.message) ? error.message : "CODEX_CONNECTION_FAILED" }).catch(() => undefined);
        throw error;
      } finally {
        clearInterval(heartbeatTimer);
        clearTimeout(timeout);
        await protocol.close();
        currentProtocol = undefined;
      }
      if (once) return;
    }
  } finally {
    process.removeListener("SIGINT", shutdown);
    process.removeListener("SIGTERM", shutdown);
    await currentProtocol?.close();
  }
}
