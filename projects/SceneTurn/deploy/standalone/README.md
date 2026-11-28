# SceneTurn 单机部署

Compose 从已构建的 Linux 镜像启动网页和接口。使用同一源码版本从 `frontend/Dockerfile` 构建网页镜像，从 `scripts/stage_backend_image_context.py` 生成的目录构建接口镜像，具体命令见[项目 README](../../README.md)。在部署目录将 `.env.example` 复制为权限为 `0600` 的 `.env`，填写实际镜像引用、入口所在 Docker 网络、网页 HTTPS 来源地址和 SceneTurn 专用代理密钥。来源地址仅填写协议、主机和可选端口。代理密钥使用本机随机生成的值，与共享入口持有的 SceneTurn 密钥一致。

共享入口通过所填网络访问 `sceneturn-web:3000` 与 `sceneturn-api:8080`。入口把同源的 `/api/v1` 和下级路径送至接口服务，覆盖客户端提供的 `X-Client-IP` 和 `X-Proxy-Secret`。这两个值分别来自入口实际核实的来源地址和 SceneTurn 的代理密钥。其他页面和资源发往网页服务。两个容器均通过 Docker 网络通信。登录 Cookie 仅经 HTTPS 发送。

从部署目录运行 `docker compose config --quiet` 校验配置，再运行 `docker compose up -d --wait`。服务健康后验证首页、注册登录、项目隔离、素材分段读取和重建容器后的项目恢复。保持一个接口容器执行本机任务。接入前后用独立账号核验权限和登录限流，确认迁移中的模型任务没有重复提交。

`sceneturn_data` 命名卷保存数据库、媒体、导出文件和本机加密主密钥。首次迁移应在原服务停写后取得一致的数据库与文件快照，将快照恢复到目标卷并确保接口容器用户 `10001:10001` 可读写。原实例若通过环境变量持有主密钥，在目标卷的 `secrets/master.key` 安全安装同一密钥，文件权限为 `0600`。保留切换前的加密备份，验证目标可解密原有配置后再切换流量。普通项目 ZIP 保留单个作品数据。账号、会话和模型凭据随完整数据卷迁移。

停止服务可用 `docker compose stop`。升级时保留上一版镜像和升级前的数据卷备份。涉及数据库升级时，在备份副本上验证旧版与新版的兼容情况后操作。Linux 镜像目前缺少 macOS 的系统旁白音色。
