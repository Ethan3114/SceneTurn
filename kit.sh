#!/usr/bin/env bash
# SceneTurn 开发流程入口。
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
exec bash "$root/tooling/idea-to-launch/kit.sh" "$@"
