import { spawn, execFileSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { EventEmitter } from "node:events";
import { dirname, resolve } from "node:path";
import { findPackageJSON } from "node:module";
import { JSONRPCClient } from "json-rpc-2.0";
import { z } from "zod";
import parser from "stream-json/jsonl/parser.js";


export const SUPPORTED_VERSION = "0.168.0";
const bridgePackage = findPackageJSON(import.meta.url);
if (!bridgePackage) throw new Error("BRIDGE_PACKAGE_NOT_FOUND");
export const codexExecutable = resolve(dirname(bridgePackage), "node_modules/.bin/codex");
const allowedMethods = new Set(["initialize", "thread/start", "thread/resume", "thread/turns/list", "thread/items/list", "turn/start", "turn/interrupt", "account/read", "model/list", "mcpServerStatus/list", "skills/list"]);
const wireMessage = z.object({
  id: z.union([z.string(), z.number()]).optional(),
  method: z.string().optional(),
  params: z.unknown().optional(),
  result: z.unknown().optional(),
  error: z.object({ code: z.number(), message: z.string(), data: z.unknown().optional() }).optional(),
}).passthrough();

export const dynamicCallSchema = z.object({
  arguments: z.unknown(), callId: z.string(), threadId: z.string(), turnId: z.string(), tool: z.string(),
  namespace: z.string().nullable().optional(),
});

export type DynamicCall = z.infer<typeof dynamicCallSchema>;

export function installedVersion(): string {
  const result = execFileSync(codexExecutable, ["--version"], { encoding: "utf8", timeout: 10000 }).trim();
  const version = result.replace(/^codex-cli\s+/, "");
  if (version !== SUPPORTED_VERSION) throw new Error("CODEX_VERSION_UNSUPPORTED");
  return version;
}

export class CodexProtocol extends EventEmitter {
  readonly process: ChildProcessWithoutNullStreams;
  readonly client: JSONRPCClient;
  private closed = false;
  private readonly parser;
  private readonly pending = new Set<string | number>();

  constructor(cwd: string, readonly imageProfile?: { inputsPath: string }) {
    super();
    installedVersion();
    const disabled = ["shell_tool", "apps", "plugins", "remote_plugin", "multi_agent", "multi_agent_v2", "browser_use", "computer_use", "view_image", "image_generation", "hooks", "plugin_hooks", "in_app_local_automation", "sleep_tool", "skill_search", "code_mode", "code_mode_only", "code_mode_only_strict_3p_tools"];
    const args = ["app-server", "--stdio", "-c", "mcp_servers={}", "-c", "mcp_servers.node_repl.enabled=false", "-c", "web_search=\"disabled\"", "-c", "shell_environment_policy.inherit=\"none\"", "-c", "project_doc_max_bytes=0", "-c", "include_environment_context=false"];
    for (const feature of disabled) args.push("-c", `features.${feature}=${feature === "image_generation" && imageProfile ? "true" : "false"}`);
    args.push("-c", "features.code_mode_host=true");
    if (imageProfile) {
      if (resolve(cwd, "inputs") !== imageProfile.inputsPath) throw new Error("CODEX_IMAGE_INPUT_SCOPE_INVALID");
      args.push("-c", `permissions.openfmv_image.filesystem={${JSON.stringify(imageProfile.inputsPath)}="read"}`,
        "-c", "permissions.openfmv_image.network={enabled=false}", "-c", "default_permissions=\"openfmv_image\"");
    }
    const environment = { ...process.env };
    for (const name of ["CODEX_CI", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID", "CODEX_VERSION"]) delete environment[name];
    this.process = spawn(codexExecutable, args, { cwd, env: environment, stdio: ["pipe", "pipe", "pipe"] });
    this.client = new JSONRPCClient((message) => {
      if (this.closed) return Promise.reject(new Error("CODEX_CONNECTION_CLOSED"));
      this.process.stdin.write(`${JSON.stringify(message)}\n`);
      return Promise.resolve();
    });
    this.parser = parser.asStream();
    this.process.stdout.pipe(this.parser);
    this.parser.on("data", (chunk: { value: unknown }) => { void this.receive(chunk.value).catch(() => this.fail("CODEX_PROTOCOL_INVALID")); });
    this.parser.on("error", () => this.fail("CODEX_PROTOCOL_INVALID"));
    this.process.on("error", () => this.fail("CODEX_PROCESS_FAILED"));
    this.process.on("exit", () => this.fail("CODEX_PROCESS_EXITED"));
    this.process.stderr.on("data", () => undefined);
  }

  private fail(code: string): void {
    if (this.closed) return;
    this.closed = true;
    this.client.rejectAllPendingRequests(code);
    if (this.process.exitCode === null && this.process.signalCode === null) this.process.kill("SIGTERM");
    this.emit("closed", code);
  }

  private async receive(value: unknown): Promise<void> {
    if (this.closed) return;
    const parsed = wireMessage.safeParse(value);
    if (!parsed.success) return this.fail("CODEX_PROTOCOL_INVALID");
    const message = parsed.data;
    if (message.method && message.id !== undefined) {
      if (message.method !== "item/tool/call" || this.imageProfile) {
        this.process.stdin.write(`${JSON.stringify({ id: message.id, error: { code: -32601, message: "METHOD_NOT_AUTHORIZED" } })}\n`);
        this.emit("deniedRequest", message.method);
        return;
      }
      const call = dynamicCallSchema.safeParse(message.params);
      if (!call.success || call.data.namespace) {
        this.respond(message.id, { success: false, contentItems: [{ type: "inputText", text: "TOOL_CALL_INVALID" }] });
        return;
      }
      this.pending.add(message.id);
      this.emit("toolCall", message.id, call.data);
    } else if (message.method) {
      this.emit("notification", message.method, message.params);
    } else if (message.id !== undefined) {
      this.client.receive(message.error ? { jsonrpc: "2.0", id: message.id, error: message.error } : { jsonrpc: "2.0", id: message.id, result: message.result });
    }
  }

  request(method: string, params: unknown): Promise<unknown> {
    if (!allowedMethods.has(method)) return Promise.reject(Object.assign(new Error("METHOD_NOT_AUTHORIZED"), { code: -32601 }));
    return Promise.resolve(this.client.timeout(30000).request(method, params));
  }

  async initialize(): Promise<unknown> {
    const result = await this.request("initialize", { clientInfo: { name: "openfmv_codex_bridge", title: "OpenFMV本机连接", version: "0.1.0" }, capabilities: { experimentalApi: true } });
    this.process.stdin.write(`${JSON.stringify({ method: "initialized", params: {} })}\n`);
    return result;
  }

  async assertMcpIsolated(threadId: string): Promise<{ isolated: true; checkedServers: number }> {
    const inventorySchema = z.object({ data: z.array(z.object({ name: z.string(), runtimeStatus: z.string().nullable(), tools: z.record(z.string(), z.unknown()).nullable().optional() }).passthrough()), nextCursor: z.string().nullable().optional() });
    let cursor: string | null = null;
    let checkedServers = 0;
    do {
      const inventory = inventorySchema.parse(await this.request("mcpServerStatus/list", { threadId, cursor, detail: "toolsAndAuthOnly", limit: 100 }));
      for (const server of inventory.data) {
        if (server.runtimeStatus !== "disabled" || Object.keys(server.tools ?? {}).length !== 0) throw new Error("CODEX_EXTERNAL_TOOLS_ENABLED");
        checkedServers += 1;
      }
      cursor = inventory.nextCursor ?? null;
      if (checkedServers > 1000) throw new Error("CODEX_TOOL_INVENTORY_LIMIT");
    } while (cursor !== null);
    return { isolated: true, checkedServers };
  }

  async assertRestrictedConfiguration(): Promise<{ restricted: true; dynamicToolHostEnabled: boolean }> {
    const received = await this.client.timeout(15000).request("config/read", { includeLayers: false });
    const data = z.object({ config: z.object({ features: z.record(z.string(), z.unknown()), web_search: z.string() }).passthrough() }).parse(received);
    for (const name of ["shell_tool", "apps", "plugins", "browser_use", "computer_use", "view_image", "code_mode", "code_mode_only", "code_mode_only_strict_3p_tools"]) {
      if (data.config.features[name] !== false) throw new Error("CODEX_RESTRICTION_UNVERIFIED");
    }
    if (data.config.features.code_mode_host !== true || data.config.features.image_generation !== !!this.imageProfile || data.config.web_search !== "disabled") throw new Error("CODEX_RESTRICTION_UNVERIFIED");
    if (this.imageProfile) {
      const profile = z.object({ default_permissions: z.literal("openfmv_image"), permissions: z.record(z.string(), z.unknown()) }).parse(data.config);
      const selected = z.object({ filesystem: z.record(z.string(), z.unknown()), network: z.object({ enabled: z.literal(false) }),
        description: z.string().nullable().optional(), extends: z.null().optional(), workspace_roots: z.null().optional(),
      }).strict().parse(profile.permissions.openfmv_image);
      const { glob_scan_max_depth: depth, ...entries } = z.object({ glob_scan_max_depth: z.null().optional() }).passthrough().parse(selected.filesystem);
      if (depth != null || JSON.stringify(entries) !== JSON.stringify({ [this.imageProfile.inputsPath]: "read" })) throw new Error("CODEX_IMAGE_FILESYSTEM_UNVERIFIED");
    }
    return { restricted: true, dynamicToolHostEnabled: true };
  }

  async assertChatgptAccount(): Promise<void> {
    const account = z.object({ account: z.object({ type: z.literal("chatgpt"), planType: z.string().min(1).refine((value) => value !== "free") }).passthrough() }).safeParse(await this.request("account/read", { refreshToken: false }));
    if (!account.success) throw new Error("CODEX_IMAGE_CHATGPT_REQUIRED");
  }

  respond(id: string | number, result: { success: boolean; contentItems: Array<{ type: "inputText"; text: string }> }): void {
    this.pending.delete(id);
    if (!this.closed) this.process.stdin.write(`${JSON.stringify({ id, result })}\n`);
  }

  async close(): Promise<void> {
    this.closed = true;
    this.client.rejectAllPendingRequests("CODEX_CONNECTION_CLOSED");
    if (this.process.exitCode !== null || this.process.signalCode !== null) return;
    if (!this.process.stdin.writableEnded) this.process.stdin.end();
    this.process.kill("SIGTERM");
    await new Promise<void>((resolve) => {
      if (this.process.exitCode !== null) return resolve();
      this.process.once("exit", () => resolve());
      const timeout = setTimeout(() => { this.process.kill("SIGKILL"); resolve(); }, 5000);
      timeout.unref();
    });
  }
}
