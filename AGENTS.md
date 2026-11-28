# SceneTurn 开发说明

- 项目位于 `projects/SceneTurn/`。启动、测试与部署命令见[项目指南](projects/SceneTurn/README.md)。
- 前端、桌面程序及播放器位于 `projects/SceneTurn/frontend/`，后端 API 和任务处理位于 `projects/SceneTurn/backend/`，本机 Codex 连接程序位于 `projects/SceneTurn/scripts/codex-bridge/`。
- 修改前端时查看[前端开发说明](projects/SceneTurn/frontend/AGENTS.md)。涉及项目导入、备份或播放时，核对已有格式和实际媒体的读取行为。
- 数据、凭据、模型回执和构建缓存保存在 Git 忽略的本机目录。运行测试时使用独立的数据目录。
