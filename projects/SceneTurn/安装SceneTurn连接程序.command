#!/bin/bash
set -euo pipefail
umask 077

product="$(cd "$(dirname "$0")" && pwd -P)"
package="$product/scripts/codex-bridge"
source="$package/macos"
intermediate="$product/.intermediate"
support="$HOME/Library/Application Support/SceneTurn Codex"
applications="$HOME/Applications"
destination="$applications/SceneTurn Codex.app"
identifier="online.ethan-g.sceneturn.codex-bridge"
registrar="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if /usr/bin/pgrep -u "$(id -u)" -x SceneTurnCodex >/dev/null; then
    printf '%s\n' '请从菜单栏退出 SceneTurn 连接程序，再重新安装。' >&2
    exit 1
fi
if [ -e "$destination" ] || [ -L "$destination" ]; then
    installed_info="$destination/Contents/Info.plist"
    if [ -L "$destination" ] || [ "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$installed_info")" != "$identifier" ]; then
        printf '%s\n' '应用程序目录已有同名程序，请先在 Finder 中确认。' >&2
        exit 1
    fi
fi
if ! npm_executable="$(command -v npm)" || ! node_executable="$(command -v node)"; then
    printf '%s\n' '请安装 Node.js 后重新打开安装程序。' >&2
    exit 1
fi
"$node_executable" -e 'if (typeof require("node:module").findPackageJSON !== "function") process.exit(1)' || {
    printf '%s\n' '当前 Node.js 版本无法运行连接程序，请更新 Node.js。' >&2
    exit 1
}

mkdir -p "$intermediate/codex-macos-tmp" "$applications" "$support/scripts" "$support/.intermediate/codex-macos-tmp"
export TMPDIR="$intermediate/codex-macos-tmp"
if [ ! -x "$package/node_modules/.bin/codex" ]; then
    "$npm_executable" --prefix "$package" ci --cache "$intermediate/npm-cache"
fi

build="$(mktemp -d "$intermediate/codex-macos-build.XXXXXXXX")"
trap 'rm -rf -- "$build"' EXIT
staged="$build/SceneTurn Codex.app"
info="$staged/Contents/Info.plist"
staged_package="$build/runtime/codex-bridge"
mkdir -p "$staged/Contents/MacOS" "$staged_package"
/bin/cp "$package/package.json" "$package/package-lock.json" "$staged_package/"
/bin/cp -cR "$package/src" "$package/node_modules" "$staged_package/"
/bin/cp -cR "$product/skills" "$build/runtime/skills"
/bin/cp "$source/Info.plist" "$info"
/usr/bin/plutil -replace SceneTurnProductRoot -string "$support" "$info"
/usr/bin/plutil -replace SceneTurnNpmExecutable -string "$npm_executable" "$info"
/usr/bin/plutil -replace SceneTurnNodeDirectory -string "$(dirname "$node_executable")" "$info"
(
    cd "$product"
    TMPDIR=.intermediate/codex-macos-tmp /usr/bin/swiftc -O -parse-as-library -target "$(uname -m)-apple-macosx13.0" \
        -module-cache-path "$build/module-cache" "$source/main.swift" -o "$staged/Contents/MacOS/SceneTurnCodex"
)
/usr/bin/codesign --force --sign - "$staged"
/usr/bin/codesign --verify --strict "$staged"

install_directory() {
    local new_path="$1" current="$2" previous="$3"
    if [ -e "$current" ]; then
        /bin/mv "$current" "$previous"
    fi
    if ! /bin/mv "$new_path" "$current"; then
        if [ -e "$previous" ]; then
            /bin/mv "$previous" "$current"
        fi
        return 1
    fi
}

install_directory "$staged_package" "$support/scripts/codex-bridge" "$build/previous-bridge"
install_directory "$build/runtime/skills" "$support/skills" "$build/previous-skills"
install_directory "$staged" "$destination" "$build/previous-app"
/usr/bin/codesign --verify --strict "$destination"
"$registrar" -f "$destination"
printf '%s\n' '安装完成。回到 SceneTurn 网页，点击「连接本人 Codex」。'
