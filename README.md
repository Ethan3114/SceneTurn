<img src="projects/SceneTurn/frontend/public/sceneturn/icon.svg" alt="SceneTurn 图标" width="80">

# SceneTurn

SceneTurn 是一款互动影像创作平台。创作者可以编排剧情分支和场景时间线，制作带有选择按钮和限时互动的影片。观众可以在浏览器中观看，并通过选择走到不同结局。

[在线体验](https://sceneturn.ethan-g.online/zh-CN) · [项目指南](projects/SceneTurn/README.md) · [单机部署](projects/SceneTurn/deploy/standalone/README.md) · [MIT 许可证](LICENSE)

## 创作与发布

- **剧情编排** 将场景连接成多条剧情路线，设置选择、跳转和限时互动，在编辑器内预览各条路线。
- **素材制作** 在创作画布中编写剧本、整理分镜、制作图像和视频，并将采用的素材放入场景时间线。
- **作品发布** 将固定版本发布到作品馆，或导出可在浏览器中独立运行的游戏包。完整项目支持 ZIP 备份和恢复。

《潮汐尽头》可在官网首页试玩。登录后可创建自己的作品。

## 本地运行

需要 Node.js 24、Python 3.12、`uv` 和 FFmpeg。先在仓库根目录安装依赖并启动后端。

```bash
cd projects/SceneTurn
mkdir -p .intermediate/tmp .intermediate/cache
export TMPDIR="$PWD/.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/.intermediate/cache"
uv venv backend/.venv --python 3.12
uv pip sync backend/requirements.txt --python backend/.venv/bin/python --cache-dir .intermediate/uv-cache
npm --prefix frontend ci --cache .intermediate/npm-cache
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8310 --no-proxy-headers
```

在另一个终端从仓库根目录启动前端。

```bash
cd projects/SceneTurn/frontend
export TMPDIR="$PWD/../.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/../.intermediate/cache"
npm run dev
```

打开 [本地创作台](http://127.0.0.1:3210/zh-CN/account) 注册并创建项目。环境变量、模型配置和测试命令见[项目指南](projects/SceneTurn/README.md#本机启动)。
