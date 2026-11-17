import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { BridgeHttpError } from "../src/api.js";
import { correctableToolFailure } from "../src/runtime.js";
import { productRoot } from "../src/storage.js";

test("真实互动校验拒绝超时范围，原项目保持不变，修正后只保存一次", () => {
  const result = JSON.parse(execFileSync(resolve(productRoot, "backend/.venv/bin/python"),
    [resolve(productRoot, "scripts/codex-bridge/tests/interaction_failure_case.py")], {
      cwd: resolve(productRoot, "backend"), encoding: "utf8",
      env: { ...process.env, PYTHONPATH: resolve(productRoot, "backend"), PYTHONDONTWRITEBYTECODE: "1" },
    })) as { failure: { code: string; status: number }; unchangedAfterFailure: boolean; revisionDelta: number; interactionCount: number; correctedPersisted: boolean };
  const failure = correctableToolFailure("edit_interaction", new BridgeHttpError(result.failure.code, result.failure.status));
  assert.equal(failure?.error.code, "INTERACTION_TIME_INVALID");
  assert.equal(failure?.executed, false);
  assert.equal(failure?.correctionRequired, true);
  assert.match(failure!.error.message!, /场景时间线/);
  assert.equal(result.unchangedAfterFailure, true);
  assert.equal(result.revisionDelta, 1);
  assert.equal(result.interactionCount, 1);
  assert.equal(result.correctedPersisted, true);
});

test("确定性编辑参数错误按所属工具返回明确修正说明", () => {
  const cases = [
    ["edit_interaction", "INTERACTION_TARGET_INVALID"],
    ["edit_timeline", "MEDIA_DURATION_CONFLICT"],
    ["edit_timeline", "ASSET_TYPE_UNSUPPORTED"],
    ["arrange_story", "STORY_CONNECTION_INVALID"],
    ["place_interaction", "BUTTON_RECT_INVALID"],
    ["move_timeline_clip", "TRACK_TYPE_MISMATCH"],
  ];
  for (const [tool, code] of cases) {
    const failure = correctableToolFailure(tool!, new BridgeHttpError(code!, 422));
    assert.equal(failure?.error.code, code);
    assert.equal(typeof failure?.error.message, "string");
    assert.equal(failure?.executed, false);
    assert.equal(failure?.correctionRequired, true);
  }
});

test("版本冲突、权限、人工确认、预算和未知错误继续要求暂停", () => {
  for (const code of ["REVISION_CONFLICT", "TARGET_CHANGED", "AGENT_RUN_WAITING", "TOOL_NOT_ALLOWED", "AGENT_SCENE_NOT_ALLOWED",
    "CLIP_ASSET_REPLACEMENT_REQUIRES_CONFIRMATION", "BUDGET_EXCEEDED", "TIMELINE_INVALID", "MEDIA_PLAYBACK_RATE_INVALID", "UNKNOWN_VALIDATION_ERROR"]) {
    for (const status of [403, 404, 409, 422, 500]) {
      assert.equal(correctableToolFailure("edit_interaction", new BridgeHttpError(code, status)), null);
    }
  }
  for (const status of [400, 401, 403, 404, 409, 429, 500, 503]) {
    assert.equal(correctableToolFailure("edit_interaction", new BridgeHttpError("INTERACTION_TIME_INVALID", status)), null);
  }
});

test("错误代码不跨工具生效，生成工具和非HTTP异常保持原失败路径", () => {
  for (const tool of ["generate_image", "generate_video", "quote_video", "confirm_generation", "write_artifact", "read_story", "constructor", "toString"]) {
    assert.equal(correctableToolFailure(tool, new BridgeHttpError("INTERACTION_TIME_INVALID", 422)), null);
    assert.equal(correctableToolFailure(tool, new BridgeHttpError("AGENT_TOOL_ARGUMENTS_INVALID", 422)), null);
  }
  assert.equal(correctableToolFailure("edit_timeline", new BridgeHttpError("INTERACTION_TIME_INVALID", 422)), null);
  assert.equal(correctableToolFailure("edit_interaction", new Error("INTERACTION_TIME_INVALID")), null);
  assert.equal(correctableToolFailure("edit_interaction", new TypeError("fetch failed")), null);
});

test("编辑工具结构校验失败可修正，返回内容没有请求正文", () => {
  const failure = correctableToolFailure("apply_story_ops", new BridgeHttpError("AGENT_TOOL_ARGUMENTS_INVALID", 422));
  assert.equal(failure?.error.code, "AGENT_TOOL_ARGUMENTS_INVALID");
  assert.equal(failure?.executed, false);
  assert.deepEqual(Object.keys(failure!.error).sort(), ["code", "message"]);
  assert.match(failure!.instruction, /新的工具调用/);
});
