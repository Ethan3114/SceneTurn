#!/bin/bash
# SceneTurn 开发流程的 macOS 启动入口。
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
exec bash "$root/tooling/idea-to-launch/开工.command" "$@"
