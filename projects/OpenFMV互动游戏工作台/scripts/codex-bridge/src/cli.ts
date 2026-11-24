import { parseArgs } from "node:util";
import { resolve } from "node:path";
import { z } from "zod";
import { input, password } from "@inquirer/prompts";
import { postJson, validateApiBase } from "./api.js";
import { CodexProtocol, installedVersion } from "./protocol.js";
import { runBridge } from "./runtime.js";
import { credentialSchema, prepareStateDirectory, readPrivateJson, stateDirectory, writePrivateJson } from "./storage.js";

const pairingRequestSchema = z.object({ apiBase: z.string(), pairingId: z.string(), pairingCode: z.string().min(32) }).strict();

async function interactivePairing(): Promise<z.infer<typeof pairingRequestSchema>> {
  if (!process.stdin.isTTY) throw new Error("INTERACTIVE_TERMINAL_REQUIRED");
  const apiBase = await input({ message: "工作台服务地址", default: "http://127.0.0.1:8310/api/v1", validate(value) {
    const parsed = z.string().url().safeParse(value.trim());
    if (!parsed.success) return "请输入完整的工作台服务地址";
    const url = new URL(parsed.data);
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    return !url.username && !url.password && !url.search && !url.hash && url.pathname.replace(/\/$/, "") === "/api/v1" && (url.protocol === "https:" || (local && url.protocol === "http:")) || "请使用工作台的HTTPS地址或本机服务地址，路径以/api/v1结束";
  } });
  const pairingId = await input({ message: "粘贴网页中的配对ID", validate: (value) => z.uuid().safeParse(value.trim()).success || "请完整粘贴网页中的配对ID" });
  const pairingCode = await password({ message: "粘贴网页中的配对码", mask: true, toggleMask: false,
    validate: (value) => value.trim().length >= 32 && value.trim().length <= 200 || "请完整粘贴网页中的配对码" });
  return pairingRequestSchema.parse({ apiBase: apiBase.trim(), pairingId: pairingId.trim(), pairingCode: pairingCode.trim() });
}

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({ options: { "request-file": { type: "string" }, once: { type: "boolean", default: false }, run: { type: "boolean", default: false }, help: { type: "boolean", short: "h", default: false } }, allowPositionals: true });
  if (values.help) {
    console.log("SceneTurn 本机 Codex 连接\n\n双击产品目录中的连接Codex.command，按提示完成配对并保持连接。\n\npair [--run]：交互式输入服务地址、配对ID和隐藏配对码；--run在配对后持续连接。\npair --request-file <0600文件>：使用本机受限文件配对。\nrun [--once]：使用已保存的连接，--once处理一项任务后退出。\nprobe：核对本机Codex协议，不启动模型回合。");
    return;
  }
  const command = positionals[0];
  if (positionals.length > 1 || (values.run && command !== "pair") || (values["request-file"] && command !== "pair")) throw new Error("CLI_ARGUMENT_INVALID");
  if (command === "pair") {
    const request = values["request-file"] ? pairingRequestSchema.parse(await readPrivateJson(resolve(values["request-file"]))) : await interactivePairing();
    const apiBase = validateApiBase(request.apiBase);
    const version = installedVersion();
    const claimed = z.object({ token: z.string(), connection: z.object({ id: z.string(), projectId: z.string(), expiresAt: z.string() }).passthrough() }).parse(await postJson(apiBase, `/connections/pairings/${encodeURIComponent(request.pairingId)}/claim`, { pairingCode: request.pairingCode, clientVersion: version }));
    const credentials = credentialSchema.parse({ apiBase, connectionId: claimed.connection.id, projectId: claimed.connection.projectId, token: claimed.token, expiresAt: claimed.connection.expiresAt, version });
    await writePrivateJson("credentials.json", credentials);
    if (values["request-file"]) console.log(JSON.stringify({ paired: true, connectionId: credentials.connectionId, projectId: credentials.projectId, expiresAt: credentials.expiresAt }));
    else console.log("配对成功，连接凭据已安全保存在本机。");
    if (values.run) {
      console.log("已连接工作台。请保持此窗口打开，在网页中发起本人Codex任务；按Control+C结束连接。");
      await runBridge(credentials, false);
    }
  } else if (command === "run") {
    await prepareStateDirectory();
    const credentials = credentialSchema.parse(await readPrivateJson(resolve(stateDirectory, "credentials.json")));
    await runBridge(credentials, values.once);
  } else if (command === "probe") {
    await prepareStateDirectory();
    const protocol = new CodexProtocol(stateDirectory);
    try {
      await protocol.initialize();
      const account = z.object({ account: z.unknown(), requiresOpenaiAuth: z.boolean().optional() }).passthrough().parse(await protocol.request("account/read", { refreshToken: false }));
      const denied: string[] = [];
      for (const method of ["thread/shellCommand", "command/exec", "process/spawn"]) {
        await protocol.request(method, {}).then(() => { throw new Error("UNAUTHORIZED_METHOD_ACCEPTED"); }, (error: unknown) => {
          if (!(error instanceof Error) || error.message !== "METHOD_NOT_AUTHORIZED") throw error;
          denied.push(method);
        });
      }
      console.log(JSON.stringify({ version: installedVersion(), initialized: true, accountPresent: account.account !== null, denied, modelTurnsStarted: 0 }));
    } finally { await protocol.close(); }
  } else {
    throw new Error("使用 pair --request-file <0600文件>、run [--once] 或 probe");
  }
}

void main().catch((error: unknown) => {
  if (error instanceof Error && error.name === "ExitPromptError") {
    console.log("配对已取消。");
    process.exitCode = 130;
    return;
  }
  const message = error instanceof Error ? error.message : "BRIDGE_FAILED";
  const messages: Record<string, string> = {
    PAIRING_INVALID: "配对ID或配对码无效，请从当前项目的连接设置重新复制。",
    PAIRING_EXPIRED: "配对请求已过期，请在网页中重新创建配对。",
    PAIRING_ALREADY_CLAIMED: "该配对请求已使用，请在网页查看连接或创建新的配对。",
    CONNECTION_REVOKED: "连接已撤销，请在网页中创建新的配对。",
    CONNECTION_EXPIRED: "连接已到期，请在网页中重新配对。",
    CODEX_VERSION_UNSUPPORTED: "本机连接程序与工作台版本不匹配，请使用本产品附带的连接程序。",
    INTERACTIVE_TERMINAL_REQUIRED: "请双击产品目录中的连接Codex.command完成配对。",
    REVISION_CONFLICT: "项目已有新的编辑，请回到网页核对当前版本后恢复任务。",
    CLI_ARGUMENT_INVALID: "启动参数无效，请直接双击连接Codex.command。",
  };
  console.error(messages[message] ?? "连接已停止，请查看工作台中的连接与任务状态。");
  process.exitCode = 1;
});
