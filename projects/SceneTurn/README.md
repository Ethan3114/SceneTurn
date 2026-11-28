# SceneTurn 互动影像创作平台

[打开 SceneTurn 正式网站](https://sceneturn.ethan-g.online/zh-CN)。首页可试玩《潮汐尽头》，登录后进入创作台。[单机部署](deploy/standalone/README.md)

SceneTurn 用于制作和发布带有剧情分支的互动影片。创作者可以编排场景和时间线，在画布中制作图片、视频和音频，设置选择按钮及限时互动，再把固定版本发布到作品馆。观众可以直接在浏览器里观看、选择和重试。

项目支持完整备份、ZIP 导入和独立游戏包导出。素材工作台支持通过 DeepSeek、商汤、阿里云万相等服务制作文本和影像。创作者也可以连接个人 Codex，按项目授权编辑剧情、时间线和互动设置。

阶段2-4已验收。现有工作台提供项目导入、完整备份、编辑器试玩和网页游玩。剧情编排通过问问AI连接本人Codex，操作故事、时间线、按钮和QTE。素材入口采用总编对话、岗位交接和成果分区，平台文字使用DeepSeek Flash，图片使用商汤sensenova-u1.5-fast，视频使用北京wan3.0-video。连续视频使用n+1张边界图片和n段固定首尾帧视频，候选采用保留原裁切和互动。用户可在项目列表和固定版本记录中直接进入游玩页面，游戏ZIP可以另行下载。两个原工程实际导出的互通、导入映射、网页游玩及重启证明已保存。

阶段3-1实现首页、作品馆、作品详情、网页播放器和模型设置。首页支持两条分支试玩、功能预览切换与折叠问答。在项目「导出 → 项目备份与游戏导出」确认固定版本后，可以选择作品馆发布、封面和先导片。首页先导片通过启动环境的 `SCENETURN_PREMIERE_PUBLICATION_SLUG` 指定含真实先导片的公开作品。[SceneTurn 产品设计总稿](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78) · [阶段3-1照着做清单](docs/evidence/阶段3-1/照着做清单.md)

阶段3-2使用 1280 和 1440 像素宽度验证桌面工作台。隔离项目使用真实成片，检查保存冲突、键盘试玩、媒体读取、固定版本、备份恢复和独立游戏包。[阶段3-2操作清单](docs/evidence/阶段3-2/照着做清单.md)

《裂盏》剧情样例包含三场成片。「夜试斗气」的两个选项分别进入「稳住斗气」和「裂盏失手」，后者可返回开场重试。网页游玩、备份、导入与重启复核见[阶段 2-4 验收清单](docs/evidence/阶段2-4/照着做清单.md)。

## 源码与项目备份

项目代码位于 `projects/SceneTurn/`，仓库根目录包含验证脚本和操作手册。通过完整项目备份可以在其他电脑恢复创作数据。

当前完整剧情样例以《斗破苍穹》为题材，先查找已有剧情与媒体素材，再设计开头、中间剧情、选择点和两条后续线路，确认后细化每段视频制作清单。视频生成前的图文方案、实际图片及修正由代理自主判断并通过平台执行，视频生成前由本人核对首尾帧、镜头内容和参数并确认。

[《裂盏》剧情与画面基准](docs/剧情设计/裂盏-剧情与画面基准.md)记录三个剧情场景、普通选择与失败后果，以及参照《缘起》早期动画的七张连续边界图设计。归档样例《戒中人》附有[素材来源](docs/剧情设计/斗破苍穹-素材来源.md)及[平台制作验收](docs/剧情设计/斗破苍穹-平台制作验收.md)。

## 本机预览服务

macOS当前登录会话内，前端3210与后端8310由launchd管理，进程退出后按KeepAlive设置恢复。前端使用独立构建目录，后端继续使用data/。配置和运行日志保存在.intermediate/run/，启动配置只包含普通环境变量。

在本产品目录执行状态检查：

```bash
export TMPDIR="$PWD/.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/.intermediate/cache"
export PYTHONPYCACHEPREFIX="$PWD/.intermediate/cache/python"
backend/.venv/bin/python scripts/preview.py status
```

注销或重启电脑后重新启动当前预览：

```bash
backend/.venv/bin/python scripts/preview.py start --web-root "$PWD/.intermediate/sceneturn-preview"
```

更新预览时，先运行 `backend/.venv/bin/python scripts/preview.py stop`，再运行 `npm --prefix frontend run build`。将 `frontend/.next/standalone/` 的内容放入新的 `.intermediate/` 子目录，并将该目录传给 `--web-root`。重复启动相同配置会继续使用现有进程。端口占用时，启动命令会报告冲突的端口。

正式网页的容器入口是 `frontend/Dockerfile`。在本产品目录运行 `docker build --platform linux/amd64 -t sceneturn-web frontend` 构建，镜像服务端口为 3000。启动时给网页容器设置 `BACKEND_URL`，由平台向前后端分别注入相同的 `PROXY_SHARED_SECRET`。构建过程无需生产配置。本机预览默认连接 8310。生产数据库、媒体持久存储和 HTTPS 的验收安排在阶段 4。

阶段 4 的后端容器可在本机用独立 SQLite 数据卷验收。先生成仅包含运行文件的构建目录，再以该目录构建，防止部署工具上传项目数据或本地配置。在本产品目录执行：

```bash
backend_context="$(backend/.venv/bin/python scripts/stage_backend_image_context.py)"
docker build --platform linux/amd64 -t sceneturn-api:local "$backend_context"
```

后端容器监听 8080，包含 FFmpeg、ffprobe、中文字幕字体和游戏导出脚本。线上通过服务环境变量 `SCENETURN_MASTER_KEY` 注入固定的 Fernet 主密钥，供供应商凭据加密和 Codex 配对使用。同一环境重新部署或实例回收后使用相同密钥。本机预览读取数据目录中的主密钥。

共享 CloudBase PostgreSQL 使用 HTTPS 网关。初始结构、行级权限和网关校验可在 `backend` 目录使用 Alembic 离线导出。输出用于新建独立的 `sceneturn` schema。开发预览仍使用本机数据库。正式网关切换需要完成业务路由和后台任务接入。

~~~bash
.venv/bin/python -m alembic -x deployment=cloudbase_gateway upgrade head --sql
~~~

登录后可在「我的项目」新建项目或进入独立的导入恢复页面。项目顶部可在「剧情编排」「时间线」「素材」之间切换。

在「素材 → 生成素材」进入整屏工作台，左侧选择剧情场景并查看本轮结果，右侧与总编对话。总编复述需求，创作者答复关键问题后进入派工。编剧交付可拍摄的剧本，角色、场景与分镜岗位接续已采用成果，视频师绑定实际首尾图。后续消息沿用当前任务。发起新一轮时，可选择项目默认值或待用模型。内部提示词和费用在后台保留。对话最多引用五张本项目图片。「核对视频镜头」打开首尾帧、动作、运镜、声音和规格，提交前仍需本人确认。符合恢复条件的暂停任务可在「提高模型请求上限」中确认新次数，任务沿用原费用上限继续。

「模型设置」集中配置文本、图片与视频服务及项目默认型号。「素材 → 生成素材」对话下方的「新一轮模型」用于调整下一轮制作。个人 Codex 与商汤 Fast、Seedream 5.5 同列为图片模型。通过本项目的「问问 AI」连接支持图片生成的 Codex 后即可选择，确认生成时明确授权使用个人账户。火山引擎的模型按账号授权和已核实的计费规则开放，连续视频仅使用支持固定首尾帧的型号。

「素材 → 生成素材 → 手动编辑画布」的左侧「添加节点」可创建文本、脚本、图片、视频、智能剪辑和音频素材节点，也可上传或从生成历史添加已保存的媒体。点选图片或视频可在卡片下方修改提示词、模型及规格。输出端点支持点击或拖线创建下游节点，右侧「资产管理」可检索本场景节点和项目素材。手工分镜表可保存镜头时长、画面、动作与对白，保存的镜头文字可连接到后续生成节点。[LibTV 画布与生成体系调研](docs/设计参考/LibTV画布与生成体系调研.md)

万相 3.0 新建节点的模式为「文生视频」「全能参考」「首尾帧」「视频续写」。全能参考可选用项目视频、最多十张图和本段镜头文字。已有视频编辑任务继续读取保存时的执行方式。切换模式时保留连线。点击「核对生成内容」查看本段素材、提示词和规格。相邻分镜可共用同一张边界图。

「导演台」支持导入模型、设置机位及关键帧，并把截图和无声视频送回画布。「音频」可调用本机系统音色录制旁白，「智能剪辑」可裁切、排序、添加字幕和混音。音乐、音效和音色设计需要配置对应的本机模型。各项操作的验证范围见 [素材画布逐项核对](docs/设计参考/LibTV素材画布逐项适配核对.md)。

节点名称、实际媒体规格及操作菜单放在预览上方，连线端口在卡片外。生成节点通过「生成功能」切换用途，底部显示主要参数和操作按钮。[Figma 手动素材画布](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=0-5)

已经开始制作的素材对话中，发送消息会接在当前任务上。创作者可以询问进度、补充要求或说“继续”，原成果和确认卡会随任务保留。视频请求结果待核实时，对话会显示原任务状态。明确开启另一轮制作使用“开始新一轮制作”。

视频提交结果待核实且缺少百炼任务编号时，可以用「核对首尾帧并准备再次生成」打开一次独立确认。提交由「确认再次生成」触发，原请求和费用预留继续保存，待核实的记录可与其他任务并列显示。万相提交的发送和响应等待默认各 90 秒，可通过 `SCENETURN_WAN_SUBMIT_TIMEOUT` 设置。

分镜可从本轮已选择的参考图片中复用原图作为边界帧。确认卡分别列出本轮需生成的图片数和已复用的原图。新图片登记后，视频按实际首尾帧的素材标识和文件摘要绑定。相邻剧情片段可以共用前段尾帧，两条分支可以共用同一入口画面。

素材交互测试为frontend/tests/browser/material-workspace.spec.ts，连续首尾帧及候选测试位于media-candidates.spec.ts，完整项目及前端网页游玩测试位于project-transfers.spec.ts。素材岗位使用 DeepSeek Flash 的 JSON 响应模式，每次验证并执行一个岗位操作。采用的成果和已完成的读取回执可在任务恢复后继续使用。提交结果待核实的文字请求需要本人明确确认重试，原请求及预留继续计入原任务费用上限。scripts/recover_material_task.py提供管理员本人任务恢复及重启核验；retry-unknown必须提供原call-id、confirmed和max-steps，extend-limit在本人确认后提高请求次数，平台最高60次，费用上限保持。每次snapshot和record使用新的capture-id，原证明与登记记录保留。scripts/smoke_materials.py验证真实Flash岗位交接和引用改稿；scripts/smoke_project_transfers.py复用实际模型成片验证备份、固定版本及网页游玩。阶段是否表的正式六项结果由流程管家自检输出确定。

## 本机启动

在「我的项目 → 导入与恢复完整项目」可导入 SceneTurn 完整备份、包含 `project.json` 和 `assets/` 的互动项目 ZIP，以及无限画布第 3 版完整 ZIP。上传后可核对场景、节点、媒体和缺失项目。确认后创建独立项目。重复导入同一版本会复用已有项目和素材。导入记录的 `source-history` 接口可读取原始项目和历史。

编辑器顶部“试玩”直接运行当前编辑内容，退出后继续编辑；“我的项目”中点击“在网页游玩”进入/zh-CN/play/<项目标识>，播放已保存的项目。编辑器“导出 → 项目备份与固定版本”提供完整备份和私有固定版本；完成记录中的“在网页游玩”进入/zh-CN/play/<项目标识>?version=<版本记录标识>，在前端直接操作普通按钮、QTE成功与超时分支，支持重新开始、刷新和返回编辑。固定版本的内容及文件摘要保持不变。登录归属检查在后端执行。游戏包下载保留在完成记录的展开项中。

完整备份包含项目、各场景画布、全部媒体、项目版本、对话、任务、工具、记忆、来源和候选素材。备份会排除凭据、登录会话及平台设置。独立游戏包包含 `index.html`、`game.json` 和播放媒体。创作依据和内部任务随完整备份保存。

导入和导出记录持久保存，状态包含queued、processing、awaiting_confirmation、blocked、ready和failed。后台每项任务使用30秒执行租约，每5秒续期；服务中断后原租约到期可接续。缺失、变更和无法转换的内容阻止交付；目录修复后可明确重新生成原固定内容，旧文件和记录保留。媒体摘要按流读取，实际播放文件和ZIP均核对完整性。时间线保存和裁切保留毫秒精度。

相关API：POST `/api/v1/transfers/imports`（file与operationId），POST `/api/v1/projects/{project}/exports`（kind、expectedRevision、operationId），GET `/api/v1/transfers`，GET `/api/v1/transfers/{id}`，POST `/api/v1/transfers/{id}/confirm`（confirmationHash），POST `/api/v1/transfers/{id}/retry`，GET `/api/v1/transfers/{id}/download`，GET `/api/v1/transfers/{id}/play/index.html`及GET `/api/v1/transfers/{id}/source-history`。JSON结构位于backend/app/transfer_schemas.py；各接口在后端检查所属用户，错误使用error.code和error.message。

阶段2-4的机器证明保存在.intermediate/project-transfer-smoke-20261116/。scripts/smoke_project_transfers.py的prepare准备真实项目导出，deliver为本人账号保存独立验证工程，status读取状态，before和after比较实际服务重启前后内容，record核对原真实模型文件、完整项目及浏览器实际网页游玩，要求三个入口、QTE成功与超时、刷新、匿名访问拒绝及游玩零下载事件全部通过后生成记录。原供应商请求、调用时间和用量保留，工程验证不新增模型请求。本人确认没有旧作品；两个原工程实际完整导出的互通证明保存在.intermediate/native-interop-20261116/。

在终端进入本产品目录：

```bash
cd projects/SceneTurn
mkdir -p .intermediate/tmp .intermediate/cache
export TMPDIR="$PWD/.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/.intermediate/cache"
```

后端使用Python 3.12和backend/.venv。安装依赖时在本产品目录执行：

```bash
uv venv backend/.venv --python 3.12
uv pip sync backend/requirements.txt --python backend/.venv/bin/python --cache-dir .intermediate/uv-cache
npm --prefix frontend ci --cache .intermediate/npm-cache
```

开发调试前停止本机预览，然后在一个终端窗口启动后端：

```bash
backend/.venv/bin/python scripts/preview.py stop
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8310 --no-proxy-headers
```

另一个终端窗口从仓库根目录进入前端目录并启动网页：

```bash
cd projects/SceneTurn/frontend
export TMPDIR="$PWD/../.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/../.intermediate/cache"
npm run dev
```

注册与登录地址为http://127.0.0.1:3210/zh-CN/account，后端健康检查为http://127.0.0.1:8310/api/v1/health。

普通配置由 `SCENETURN_DATA_DIR`、`SCENETURN_FRONTEND_ORIGIN`、`SCENETURN_WORKER_ENABLED` 等环境变量提供。项目数据默认位于 `data/`，测试、缓存和下载素材位于 `.intermediate/`。两个目录均由 Git 忽略。

通过 `SCENETURN_TEXT_ADMIN_EMAIL` 指定文本平台管理员。本机预览从 `.intermediate/run/operator.json` 中读取 `textAdminEmail`。管理员可以在设置页面加密保存 DeepSeek API Key。设置销售计费策略后，普通账号才可以提交收费文本请求。管理员开发验证受共享预算约束。

## 节点内文本生成与媒体处理

生成节点提供文字、图像、视频分类及原项目的专门任务。输入要求、已连接引用和参数位于节点正文；节点下方展示历史任务、真实用量与必要的媒体费用确认。

文字包含剧本创作、剧本摘要、角色画像、道具提示词、场景说明、分镜情节和单镜提示词，使用服务端DeepSeek API。任务保存真实请求标识、输入与输出Token、缓存命中及未命中Token、价表版本和供应商预估成本。平台销售价格与供应商成本分别管理，供应商最终费用以其账单为准。输出保留结构化字段，文字结果可以继续编辑并连接到下一个生成节点。

工具栏通过“生成”统一创建生成节点，具体文字、图像与视频功能在节点内部选择。点击节点正文或名称即可选中，点击节点顶部删除按钮并确认可移除该节点及其连线；非文字编辑状态也可按Delete或Backspace删除当前选中节点。取消确认保留原内容，删除画布节点继续保留生成记录、原始素材和历史版本。输入框或正文编辑中的按键继续用于文字编辑。

图片节点的“切片”支持1至12行列、拖动切线、撤销与重做。后端从当前项目读取原图，逐片保存PNG素材、Image节点、来源连线与项目版本，保留原图。

视频分类中的“拼接成片”按连线和组合成员顺序读取2至100段当前项目视频，使用本机FFmpeg统一画面尺寸并保留声音，无音轨片段补充静音。拼接结果保存为独立Video节点，可继续播放、下载与采用；本地切片与拼接不调用收费模型。

## 真实视频生成

1. 在网页注册并登录，创建项目和剧情场景。
2. 点击顶部“视频设置”，填写本人阿里云业务空间ID、API Key和北京地区配置，点击“保存生成配置”。密钥通过登录会话写入后端加密存储，设置响应只显示配置状态。
3. 在画布工具栏点击“文字”，双击节点正文，填写画面描述。点击“生成”创建Config节点，从文字节点右端拖动连线至Config左端。
4. 在生成节点中选择“视频”和“分镜视频”，直接在节点内填写镜头要求，输入@并选择已连接的文字；点击节点内“视频参数”设置时长和清晰度。
5. 点击“保存输入并查看报价”，在节点下方核对实际完整输入、参考文字、模型、参数、数量和最高费用，再点击费用确认按钮提交真实任务。
6. 页面在节点中展示真实任务状态。完成后，在独立Video结果节点播放视频，点击“采用到固定场景片段”，进入场景时间线继续编辑并保存。

已有Video来源可通过“编辑生成配置”继续编辑原提示词。平移、缩放只产生视口待保存状态，费用报价继续保持可核对和确认；点击“保存画布”会保存视口并更新项目版本，原报价随后需要重新取得。

单次生成最高10元，本次模型验证累计最高300元，每月新增云费用最高100元。达到预算上限停止新增收费请求。支付保留扩展接口，未配置资金操作返回PAYMENT_NOT_CONFIGURED。

## 自动化验证

阶段2-2在剧情画布提供“创作助手”节点。角色、目标范围、任务输入、进度、用量、停止和恢复均在节点内操作；“项目记忆与来源”保存全局设定及带分支的剧情事件，并提供原始版本查看。

交付类型默认“完整专业产物”，按岗位严格保存结构与完整正文，视觉岗位同时保存独立生图提示词；“局部文字修订”需要授权已有文字节点，“执行工具操作”用于已开放的实际处理与核验。DeepSeek角色任务通过结构化决策提交计划、执行项目工具和完成步骤；参数修正次数计入原任务请求数及费用上限。本人Codex执行同一套服务端权限与专业产物校验。

新平台角色任务使用DeepSeek Flash及[官方JSON Output](https://api-docs.deepseek.com/guides/json_mode/)，接口为`https://api.deepseek.com/chat/completions`，设置response_format为json_object、tool_choice为none；实际请求包含完整岗位业务Schema和本阶段响应Schema。每次冻结请求模型、协议、toolCatalogVersion、供应商地址、businessTools、responseSchema、空tools清单及完整requestBody。当前R2工具目录包含专业结构读取与字段补丁；历史R1按其冻结目录复核。供应商原始assistant.content与执行器真实user.toolResult形成后续消息，操作关联使用实际AgentCall.id。原始回复、实际响应模型与确定性解码结果分别保存，价格按冻结的请求模型核算；服务端严格校验长度、列表数量、来源、权限及专业交付条件。历史任务按各自冻结模型、协议、原始Schema与请求复核。模型单次输出上限32768 Token，单次HTTP超时600秒。Pro任务的历史数据保持原值，新的Pro供应商请求未获授权。

本人 Codex 连接使用本工程固定的官方 CLI 0.168.0。Mac 在 Finder 双击 [安装 SceneTurn 连接程序](安装SceneTurn连接程序.command) 完成本机安装后，打开正式站项目的「剧情编排 → 问问 AI」，点击「连接本人Codex」即可唤起菜单栏程序，浏览器要求访问本机设备时选择允许。网页显示「本人Codex已连接」后可以发送项目任务。[打开正式站项目](https://sceneturn.ethan-g.online/zh-CN/projects)

正式站通过浏览器把一次性配对信息交给本机程序，连接只授权当前项目。配对码有效期 5 分钟，网页创建的连接有效期 1 小时。Mac 菜单栏的「退出连接程序」会停止本机连接。Codex 登录凭据保存在本机。

本机预览及手动配对使用 [连接Codex.command](连接Codex.command)。网页选择「手动配对」后按终端提示填写服务地址、配对标识和配对码。正式站手动连接的服务地址为 `https://sceneturn.ethan-g.online/api/v1`，本机预览使用 `http://127.0.0.1:8310/api/v1`。

连接程序首次安装与本机检查在本产品目录执行：

```bash
npm --prefix scripts/codex-bridge ci --cache .intermediate/npm-cache
npm --prefix scripts/codex-bridge run typecheck
npm --prefix scripts/codex-bridge test
```

平台实际创作验证使用scripts/smoke_agents.py，固定DeepSeek Flash，九个角色分别最多预留10元，总预留最多90元并计入已确认的300元主预算。每个角色使用独立真实项目，共用本次验证作者提供的人物服装、携带物位置和怀表视觉设定。scripts/smoke_codex.py使用本人Codex账户，账户费用未知。两者实际重启后自动登记阶段2-2证据，不提交图片或视频生成。本轮后续专业验证方式已由本人确定为Flash。

以下命令只检查专业契约、协议Schema、输出容量及预算，不读取凭据或调用模型：

```bash
backend/.venv/bin/python scripts/smoke_agents.py --check-contracts
backend/.venv/bin/python scripts/smoke_agents.py --roles character_designer --check-contracts
```

真实创作验证命令：

```bash
backend/.venv/bin/python scripts/smoke_agents.py
backend/.venv/bin/python scripts/smoke_codex.py --timeout 600
```

平台角色验证默认每个角色等待1800秒。使用--roles指定角色可进行真实协议兼容验证，费用按实际角色数每个最多预留10元；该结果保存selectedRoles与fullStageCoverage。阶段验收要求九类角色的专业样例齐备及本人内容评分。

已有验证通过后，可使用各自的--verify-run <验证ID>只读复核原请求、费用、历史、证明与样例文件，复核不会启动模型或改写原记录。

已结束且尚未登记的真实平台验证可使用scripts/smoke_agents.py --recover-run <验证ID>登记实际调用与费用。脚本核对原请求、SQLite任务和主预算，部分验证保持completed=false与restartVerified=false；重复执行只读核对原证明。

专业产物可按真实审阅反馈发起独立修订验证。--revision-from接受运行完成且实际重启核验通过的专业证明，可为明确角色子集；任何失败或未核验重启的证明均被拒绝。--roles明确本次岗位范围，--feedback-file提供schemaVersion、sourceVerificationId及按角色排列的反馈列表。修订使用新的隔离项目与费用预留，当前授权目标保存原完整正文、原规范artifactDraft和归档来源，明确原引用尚未在当前项目验证。模型实际调用read_artifact取得完整结构、基准SHA、版本和允许路径，默认patch方法使用patch_artifact提交字段test/replace；缺少完整人物对象等情况显式指定--revision-method write，由write_artifact保存完整新专业候选。两种方法分别固定真实工具与验证要求，不自动切换。成熟JSON Patch库复算字段修改，完整专业结构、来源、正文、提示词和历史均严格核验；原文版本、全部实际调用和新候选保留，内容仍需本人评分。

以下反馈修订预检不调用模型：

```bash
backend/.venv/bin/python scripts/smoke_agents.py --revision-from <来源证明ID> --feedback-file .intermediate/agent-revision-feedback-20261113.json --roles writer character_designer --check-contracts
```

使用相同参数并移除--check-contracts会提交真实修订任务，按所选角色分别最高预留10元，计入累计300元限制。scripts/review_agent_samples.py可只读汇编当前九角色样例：

```bash
backend/.venv/bin/python scripts/review_agent_samples.py --verification-id <原九角色证明ID> --current --revision-id <修订证明ID> --select writer <修订证明ID> --select character_designer <修订证明ID>
```

其余角色使用原九角色版本；每个角色的来源证明、实际写入调用、产物与正文SHA均写入新manifest.json和完整HTML。多轮修订沿来源链核验，全部原始及修订调用按真实callId去重计费，原报告保持原样，内容评分状态单独保存。

各角色独立验证时，使用--independent-sources并明确提供九项--select <角色> <证明ID>汇集当前版本。九类岗位必须完整齐备，每份来源均须运行完成、真实重启通过，并核验其自身的完整修订基准；所有来源及祖先调用去重计费，原证明保留各自实际覆盖范围。单角色通过只代表该角色，阶段验收仍要求九类专业产物与本人评分。

真实角色验证保存完整结构化产物、中文正文、独立生图提示词、实际工具回执、项目版本、用量与费用，样例位于相应验证目录的samples.html。write_artifact按实际角色严格校验；单次响应最多32768 Token，任务费用限制及媒体逐次确认继续生效。内容质量由本人核对可直接使用程度后评分。

前端真实测试、类型检查与Lint在本产品目录执行：

```bash
npm --prefix frontend run test:run
npm --prefix frontend run typecheck
npm --prefix frontend run lint
```

当前画布浏览器用例在网页与API运行时执行：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.intermediate/playwright"
npm --prefix frontend run test:browser:quote
```

该组用例检查注册登录、画布保存、节点及引用、三种交付类型、来源、停止、恢复与三种宽度，使用真实接口与数据，不提交收费模型请求。

在本产品目录执行真实HTTP、SQLite、权限和媒体验证：

```bash
backend/.venv/bin/python scripts/verify_api.py
```

测试会启动独立真实服务，在.intermediate内创建实际账号、项目、数据库和文件。不会写入本人真实数据，不提交模型生成。视频上传与范围播放使用MDN的CC0视频flower.mp4，缓存于.intermediate/test-media/；素材来源为[MDN媒体示例](https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4)，该文件仅用于真实媒体上传与解码检查。

每个真实 SQLite 回归用例在独立目录读写副本，用例退出时释放连接并删除副本。自动验证命令结束后清理该轮临时目录。直接在 backend 内运行 pytest 时，成功用例的临时目录自动清理，失败用例可在 .intermediate/pytest-default 查看诊断文件。验证所用的原始数据库快照和真实模型回执单独保存。

本机HTTP测试、健康检查与重启检查直接连接127.0.0.1，运行时独立于系统代理设置。外部服务与素材下载沿用各自连接配置。阶段自检排除TypeScript自动生成的.tsbuildinfo缓存，继续检查实际源码、测试和验证脚本的更新时间。

文本素材使用真实.txt和.md内容，分别验证UTF8、UTF16和GB18030：完整保存content与encoding、返回原始字节及正确的Content-Type charset。错误MIME、声明编码不符、二进制内容和超过10MiB的文本由真实接口明确拒绝。

文本接口的无Key检查不提交模型，使用实际账号、数据库与HTTP验证平台管理员、所属关系、结构和微元预算。图片切片测试逐像素重建原图，视频拼接测试使用可解码的真实画面和声音核对顺序、时长及静音；两者均不调用模型。

管理员已在文字设置安全保存真实DeepSeek Key后，可执行真实文本验证：

```bash
backend/.venv/bin/python scripts/smoke_text.py
```

该命令实际调用DeepSeek，默认验证通用文本、摘要、分镜情节、单镜提示词四种结构，每次输出上限512 Token。它先在主预算保存预留，在隔离数据目录中通过正式注册、设置、项目、画布和文本API执行，核对结果、Token账目及服务重启恢复，再把真实成本结算回主预算。平台Key只在本产品后端安全边界使用；本人项目与账号保持原样。真实请求自动追加docs/evidence/阶段2-1/文本冒烟记录.md；实际重启前后核对同一任务、历史、账本和结果，通过后自动登记正式冒烟记录。提交结果待核实时保留原预留，禁止自动重新请求。

生成输入测试通过正式接口保存文字、Config、Group和连线，核对输入顺序、显式引用、去重、最终提示词长度及视频结果节点归属。媒体引用检查使用实际上传的CC0视频、该视频提取的图片及[MDN音频示例](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/audio)，同时覆盖content和storageKey。测试报价始终保持待确认、零预留，不提交收费生成。

旧报价兼容材料来自旧版正式服务实际创建的独立测试账号与项目，保存在.intermediate/legacy-quote/record.json。scripts/check_legacy_quote.py以只读SQLite和当前领域函数核对原字段、原始时间与确认摘要。scripts/check_real_media_reference.py --task-id <已完成真实任务ID> --data-dir data只读检查实际ready任务、所属视频节点、真实Asset及文件哈希；这两项检查不增加真实模型调用记录。

真实浏览器检查需要网页3210和后端8310已启动，并在.intermediate/playwright安装Chromium。在本产品目录执行：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.intermediate/playwright"
export TMPDIR="$PWD/.intermediate/tmp"
frontend/node_modules/.bin/playwright install chromium
node scripts/check-browser.mjs
```

浏览器实际操作注册、项目、场景和画布，刷新核对持久保存，并检查配置状态、支付状态及退出登录。自动生成390、768、1440宽度的截图至docs/evidence/阶段2-1/；检查依据DOM、真实交互与服务响应，截图供本人验收。

浏览器对30次真实键盘编辑测量input事件至画布文字更新后的双动画帧，检查P95不超过200毫秒。浏览器版本、宽度、逐次时长和P95自动保存于.intermediate/browser/<本次运行ID>/editing-performance.json；测量不会提交模型生成。

已有真实生成完成后，可在本机Node.js 26执行浏览器播放检查：

```bash
node scripts/check-browser.mjs --generated-task-id <真实任务ID> --data-dir data
```

脚本使用只读SQLite定位该任务的真实视频并核对哈希，在独立测试账号和项目中通过真实导入控件上传，保存后使用画布Video节点的实际播放器播放同源受保护URL。检查loadedmetadata、播放时间推进、seek、跳转后继续播放、无media.error及未登录访问拒绝；结果写入.intermediate/browser/<本次运行ID>/generated-playback.json。原用户项目版本与内容在检查前后保持一致，不提交模型生成。

重启恢复由工具包verify.sh调用scripts/restart-check.sh的start、write、read、stop四个动作。钩子只在RESTART_CHECK_DIR内运行真实服务，通过正式接口保存随机项目标题和会话；verify.sh终止服务并重新启动后，使用原Cookie读取同一项目。

## 已确认真实任务的冒烟验证

scripts/smoke.py只读取本人已在界面确认并实际完成的任务，不提交新的生成或采用操作。运行前，真实视频须已在网页采用至固定场景片段。默认验证方式为本机只读访问本产品data目录：

```bash
backend/.venv/bin/python scripts/smoke.py --task-id <真实任务ID> --data-dir data
```

脚本以SQLite只读模式加载真实模型记录，核对账号、项目、任务及生成素材归属、供应商任务标识、模型、用量、耗时、固定场景采用、文件哈希及真实视频解码，全部通过后自动追加docs/evidence/阶段2-1/冒烟记录.md。新组合任务使用报价时项目版本的真实历史快照核对来源原文、文字输入和结果节点；生成期间继续编辑当前文字不会改变原报价证据。脚本不创建会话，不修改任务、项目或媒体状态；账号及API鉴权由真实HTTP和浏览器测试独立验证。

真实调用按generationId和remoteTaskId登记于.intermediate/smoke/registry.json，记录任务实际开始与完成时间。同一任务复核只检查原记录和文件，保留原冒烟记录及调用次数。

正式记录支持Wan与DeepSeek多模型，脚本校验完整记录、逐任务索引和验证文件。对本人实际完成的文字任务，可在真实服务重启前后核对同一任务、输入历史、账本与正文，再首次登记；原调用时间保持不变：

```bash
backend/.venv/bin/python scripts/smoke_record_text.py --task-id <本人实际完成的文本任务ID> --capture-before-restart
```

实际停止并重新启动同一后端数据目录的服务后，执行：

```bash
backend/.venv/bin/python scripts/smoke_record_text.py --task-id <同一文本任务ID> --verify-after-restart --register
```

该验收只读取已有真实任务，不发送模型请求。重复复核不增加调用次数，也不改写正式记录、索引和原验证时间。

自动化环境也可使用已有的本人会话，将Playwright storageState JSON以0600权限保存在.intermediate内，使用--cookie-file进行正常HTTP鉴权。新组合任务同时使用--data-dir指定同一本人数据和历史目录，默认data；脚本核对HTTP账号与实际任务归属。证据记录不包含密钥、Cookie和完整提示词；缺少真实任务或模型结果时不生成冒烟记录。

## 阶段自检

阶段2-3在素材入口的视频师区域提供“连续视频与候选”。手动选择n+1张实际图片，保存n个固定首尾帧片段；每次准备产生独立待确认任务，同一操作重试保持幂等。候选采用先进行版本、归属、真实文件和裁切预检，再通过确认摘要应用。时长不足或文件不可用形成持久冲突，可以调整后重新预检或保留当前版本。本人Codex读取授权场景时能看到待处理采用；故事验证和发布前检查返回未处理冲突。

连续帧与采用API为：

- `POST /api/v1/projects/{project}/scenes/{scene}/media-sequences`
- `POST /api/v1/projects/{project}/media-sequences/{sequence}/segments/{segment}/quote`
- `GET /api/v1/projects/{project}/scenes/{scene}/media-candidates`
- `POST /api/v1/projects/{project}/media-adoptions/check`
- `POST /api/v1/projects/{project}/media-adoptions/{adoption}/apply`
- `POST /api/v1/projects/{project}/media-adoptions/{adoption}/keep`

`scripts/smoke_media_sequences.py prepare`准备具体真实视频请求；只有收到本人对这些请求的明确确认，才执行`confirm --confirmed`。`scripts/verify_media_candidates.py`分别验证实际生成采用、实际长短视频候选、裁切和互动保留、重启前后状态，并登记阶段2-3机器冒烟记录。首次`record`保存实际视频播放清单；浏览器用例`generated-video-playback.spec.ts`通过真实注册、上传及受保护媒体接口验证三段成片，保存播放和刷新恢复记录；再次`record`核对原始调用、完整媒体解码、视频画面时长及浏览器证据，单独记录原任务复核时间，原请求、调用时间和用量保持。参数`--directory`必须指向本产品`.intermediate`内的对应验证目录。普通运行不会自动提交未确认媒体任务。

素材制作通过剧情编排中的“素材 → 生成素材 → 问问 AI”进入。编剧展示剧本正文，图片岗位展示商汤实际结果；生图提示词、依据和工具明细通过受保护的素材对话`/developer`接口监控。创作问题每批最多五项，确认与修改答复绑定成果摘要并持久保存。图片生成前核对数量及尺寸，图片完成后确认成果，后续岗位才能继续。

确认卡中的修改要求会重新启动当前岗位修订，保存完整答案和参考图片；旧图片任务取消，新成果需要再次确认。当前修订范围内的旧写入及验证回执保留为历史，角色需要读取当前成果并保存实际修改。因修订尚未写入而中断的任务，通过版本、成果摘要、参考文件及费用结算检查后，可点击“继续当前任务”恢复；任务原请求次数和费用上限保持。未确认的成果、活动媒体和状态未知的请求继续阻止交接。

`scripts/material_revision_validation.py`提供48项真实任务副本回放，独立于默认后端测试。它需要本次两种失败状态的授权SQLite一致性副本；副本保留在本机，不进入仓库。缺少指定副本时明确失败。验证只在隔离数据库中执行生产工具和恢复流程，供应商请求保持关闭。在产品目录执行：

```bash
export SCENETURN_REVISION_SNAPSHOT="$PWD/.intermediate/material-revision-recovery-20261118/failed-run.sqlite3"
export SCENETURN_REVISION_PATCH_SNAPSHOT="$PWD/.intermediate/material-revision-recovery-20261118/patch-limit-run.sqlite3"
export PYTHONPATH="$PWD/backend"
export TMPDIR="$PWD/.intermediate/tmp"
backend/.venv/bin/python -m pytest -q scripts/material_revision_validation.py --basetemp="$PWD/.intermediate/pytest-material-revision"
```

商汤使用原AI Studio的`sensenova-u1.5-fast`接口，三个账号轮换领取同一条持久队列的任务。原配置通过下列命令加密导入，输出不包含凭据。导入操作不会调用模型。

```bash
backend/.venv/bin/python scripts/import_sensetime_accounts.py --source '../端侧AI-Studio/backend/.env' --email 'your-account@example.com'
```

工作台在数据库事务内领取图片任务，三个商汤账号合计同时只运行一个本机请求。提交满180秒仍无回执时，保留该次尝试记录并释放本机占用。同一张图仍待生成时，原任务排队，至少等5秒后交给其他可用账号。单张图片对同一账号最多有一次超时尝试，三个账号用尽后显示超时。收到结果并完成登记或明确失败后，也等待至少5秒再领取下一单。间隔和已尝试的账号都保存在数据库，重启后继续生效。已停止或已完成的批次不再提交，已保存的素材继续保留。本机释放时，商汤是否仍在处理原请求可能无法确认。

工作台通过凭据摘要识别相同凭据，各配置项对应不同商汤账号。商汤真实队列验证由`scripts/smoke_sensetime_queue.py`记录相邻提交间隔、私有证明和图片冒烟。`scripts/verify_image_restart.py`核对重启前后任务、文件摘要和账号占用。`scripts/smoke_material_review.py`使用真实DeepSeek Flash验证保存、暂停、通用继续接口拒绝及重启恢复，单次验证保留10元预算上限。

图片响应和PNG保存后，资产登记在新事务中重新读取项目、任务及账号状态，保留期间的人工编辑；同一图片重复登记保持幂等。服务启动会补登记`response_saved`的原始结果，再继续已确认的队列。登记失败保留响应、PNG和账号占用，页面显示明确状态；图片worker异常时健康检查返回503及`IMAGE_WORKER_FAILED`，日志只记录异常类型。

`scripts/image_registration_validation.py`提供12项真实响应副本验证。需要显式提供授权SQLite副本及已保存响应和PNG的隔离数据目录；验证不会调用供应商，也不会读取生产数据库。在产品目录执行：

```bash
export SCENETURN_IMAGE_REGISTRATION_SNAPSHOT="$PWD/.intermediate/material-revision-recovery-20261118/image-registration-run.sqlite3"
export SCENETURN_IMAGE_RESPONSE_DATA="$PWD/.intermediate/image-registration-recovery-20261118/data"
export PYTHONPATH="$PWD/backend"
export TMPDIR="$PWD/.intermediate/tmp"
backend/.venv/bin/python -m pytest -q scripts/image_registration_validation.py --basetemp="$PWD/.intermediate/pytest-image-registration"
```

新连续分镜使用R7工具目录及`schemaVersion=3`。n段视频对应n+1张边界帧，已有片段尾帧可直接作为下一段首帧复用。视频请求冻结图片标识和SHA-256，提交前核对原文件，再传入实际首尾帧。历史调用及结构按原版本读取。

在工具包根目录运行：

```bash
bash kit.sh 自检 SceneTurn
```

自检输出保存到当前阶段证据目录。真实模型尚待验证时，该项保持待验；阶段通过需本人按验收清单实际核对并明确确认。

## 许可证

SceneTurn 使用 MIT 许可证。许可证文件包含项目及所用第三方代码的版权声明。独立组件的许可证随组件提供。[MIT 许可证](LICENSE)
