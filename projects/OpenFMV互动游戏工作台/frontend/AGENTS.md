# OpenFMV Development Guide

本目录提供OpenFMV网页制作工作台，使用Next.js界面和统一后端API。Electron代码保留已有桌面能力。浏览器通过后端读取账号、项目、画布、任务和素材。

## Project Scope

- Framework: Next.js 16 App Router, TypeScript, React, React Flow
- Desktop shell: Electron
- State: Zustand；浏览器保存界面状态和缓存，项目数据以服务端为准
- Persistence: /api/v1项目服务、持久画布和受保护媒体接口；保留原有OpenFMV项目兼容代码
- Authentication: 邮箱密码注册、登录、退出及HttpOnly Cookie会话
- Database: 由产品backend/管理，前端通过API访问
- Cloud storage: 由后端媒体服务管理，云端部署按产品阶段安排执行

当前范围以产品docs/PRD/、技术适配声明和本阶段开发文档为准。所有业务写入检查项目版本；图片、视频和发布操作保留用户确认。供应商凭据通过受保护的设置API保存，浏览器不保存Key。

## Current Architecture

OpenFMV has two editing surfaces:

- `/editor` is the Blueprint graph editor. It owns story flow structure: nodes, edges, handles, rules, and node text/prompt metadata.
- `/nodes` is the node-level multi-track editor. It owns each node's independent `NodeTimeline v2`.

`NodeTimeline v2` is the primary media and interaction model. Store node media and time-based interaction behavior in `node.data.timeline`, not in legacy node-level media fields.

Timeline model rules:

- Timeline code lives in `app/_features/node-timeline/`.
- Timeline tracks are `media` and `interaction`.
- Media clips are `video`, `image`, and `audio`.
- Interaction clips are `button`, `hotspot`, `pauseGate`, and `text`.
- Timed actions are interaction-track clips with type `branch` or `variable`.
- Do not reintroduce a separate `logic` track. Put timed branching and variable actions into the interaction track and inspector.
- Do not reintroduce `AppNode.data.video`, `AppNode.data.image`, `AppNode.data.videoPlaybackId`, or `AppNode.data.videoThumbnail`.

Runtime rules:

- Existing playback/runtime should compile from `node.data.timeline`.
- Shared runtime logic lives in `shared/runtimeCore.mjs`; TypeScript-facing wrappers live in `app/_utils/graphRuntime.ts` and `app/_utils/graphRuntimeCore.d.ts`.
- Player UI lives in `app/_components/player/` and should consume runtime effects rather than reading node data directly.
- Electron export should copy and rewrite timeline clip `src` and `poster` paths, not legacy node media fields.

## Commands

```bash
npm run dev
npm run desktop
npm run desktop:dev
npm run desktop:standalone
npm run build
npm run package:desktop
npm run lint
npm run test:run
```

Run a single test file:

```bash
npx vitest --config vitest.real.config.mts tests/real/runtime-contract.test.ts
```

Run a single named test:

```bash
npx vitest --config vitest.real.config.mts tests/real/runtime-contract.test.ts -t "test name"
```

## TypeScript

- Keep TypeScript strict mode compatible.
- Use the `@/*` path alias for app-root imports where the surrounding code already does.
- Shared types live in `app/_types/index.ts`.
- Interfaces and types use PascalCase, for example `BranchRule`, `NodeType`, and `AppNode`.

## Imports

Group imports in this order, with a blank line between groups:

1. React and Next.js
2. Third-party libraries
3. Project modules

```typescript
import React, { memo, useEffect, useState } from 'react';
import Image from 'next/image';

import { NodeProps, Position } from '@xyflow/react';

import { CustomHandle } from './CustomHandle';
import { AppNode } from '../../_types';
import { useEditorStore } from '../../_store/useEditorStore';
```

## Comments

Do not add code comments unless the user explicitly requests them or a short comment is needed to clarify non-obvious logic. Type definition comments are acceptable when they explain complex fields.

## Components

Node components should be wrapped with `React.memo`:

```typescript
const StoryNode = ({ id, data }: NodeProps<AppNode>) => {
  return null;
};

export default memo(StoryNode);
```

Component file order:

1. Imports
2. Local type definitions
3. Component definition
4. Export

React Flow node rules:

- Do not hide handles with `display: none`.
- Use opacity or visibility transitions for handles.
- Add `nodrag` to inputs inside nodes.
- Add `nowheel` to textareas or scrollable form controls inside nodes.

```tsx
<div className="opacity-0 transition-opacity group-hover:opacity-100">
  <CustomHandle type="source" position={Position.Right} />
</div>

<input className="nodrag" />
<textarea className="nodrag nowheel" />
```

## Error Handling

Use `try`/`catch` for async user-facing operations and log useful failures.

```typescript
try {
  const result = await someAsyncOperation();
  if (result.error) {
    console.error('Operation failed:', result.error);
    alert('Operation failed: ' + result.error);
  }
} catch (error) {
  console.error('Operation failed:', error);
  alert('Operation failed');
}
```

## Styling

- Use Tailwind CSS.
- Custom colors are defined in `app/globals.css`.
- Use the `openfmv-*` color namespace where applicable.

```tsx
<div className="border-openfmv-border bg-openfmv-node text-openfmv-text" />
```

## Directory Map

```text
app/
  _components/          React components
    nodes/              React Flow node components
    editor/             Editor UI components
    player/             Player components
    local/              Local desktop UI components
    ui/                 Shared UI primitives
  _features/            Feature modules
    node-timeline/      NodeTimeline v2 schema, UI, commands, snapping, playback
  _hooks/               React hooks
  _store/               Zustand stores
  _types/               TypeScript types
  _utils/               Utility functions
  api/                  Local Next.js API routes
  editor/               Editor page
  play/[id]/            Player page
  projects/             Project management page
  asset-studio/         Asset studio page
  assets/               Asset page
  lib/                  Core app utilities
  globals.css           Global styles and CSS variables
  layout.tsx            Root layout
  page.tsx              Home page
electron/
  main.js               Electron main process and IPC
  preload.js            Exposed preload API
  exporter.js           Exported game packager
  ai-settings.js        Local CLI AI settings and calls
shared/
  runtimeCore.mjs       Shared runtime used by player and exporter
  ipc-contract.ts       IPC contract source
messages/
  *.json                next-intl locale files
__tests__/
  unit/                 Vitest unit tests
```

## Environment

网页默认127.0.0.1:3210，/api/v1同源转发到127.0.0.1:8310。运行命令显式设置TMPDIR为本产品.intermediate/tmp；真实测试需要实际后端服务。npm start运行生产standalone服务，OPENFMV_BIND_HOST与PORT控制监听。后端凭据不进入本目录。

## Notes

- `selectedNode` should stay separate from the full node array to avoid unnecessary `PropertyPanel` rerenders.
- 浏览器素材通过所属项目的上传接口保存，使用受保护的assetId内容地址。桌面兼容代码保留原有相对路径和导出行为。
- File upload/import limits should stay aligned with desktop performance constraints.
- Blueprint asset binding should only write compatible node metadata such as text or prompts. Media assets belong in `/nodes` timeline clips.
- When changing timeline schema or runtime compilation, update `__tests__/unit/nodeTimeline.test.ts` and related exporter/project persistence tests.
