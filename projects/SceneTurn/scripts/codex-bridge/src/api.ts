import { z } from "zod";
import type { Credentials } from "./storage.js";

export class BridgeHttpError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}

export function validateApiBase(value: string): string {
  const url = new URL(value);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.username || url.password || url.search || url.hash || url.pathname.replace(/\/$/, "") !== "/api/v1" || (url.protocol !== "https:" && !(local && url.protocol === "http:"))) throw new Error("API_URL_INVALID");
  return url.toString().replace(/\/$/, "");
}

export async function postJson(apiBase: string, path: string, body: unknown, token?: string): Promise<unknown> {
  if (!path.startsWith("/") || path.includes("..")) throw new Error("API_PATH_INVALID");
  const response = await fetch(validateApiBase(apiBase) + path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(path === "/connections/claim" ? { "X-SceneTurn-Skill-Format": "sceneturn-v1" } : {}),
    },
    body: JSON.stringify(body), redirect: "error", signal: AbortSignal.timeout(30000),
  });
  const value: unknown = await response.json();
  if (!response.ok) {
    const parsed = z.object({ error: z.object({ code: z.string() }) }).safeParse(value);
    throw new BridgeHttpError(parsed.success ? parsed.data.error.code : "BRIDGE_HTTP_FAILED", response.status);
  }
  return value;
}

export class ProjectApi {
  constructor(readonly credentials: Credentials) { validateApiBase(credentials.apiBase); }
  post(path: string, body: unknown = {}): Promise<unknown> {
    return postJson(this.credentials.apiBase, path, body, this.credentials.token);
  }
}
