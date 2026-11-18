# OpenFMV互动游戏工作台

在线制作互动视频游戏的工作台，包含真实注册和登录、项目及场景、剧情画布、DeepSeek文本任务与Token记录、商汤图片任务、图片切片、本地视频拼接、阿里云Wan视频任务、素材预览与采用。媒体生成逐次确认，图片按三个账号各并发1排队，视频顺序执行，创作内容和历史全量保存。当前按本人使用规模验证，平台Token销售使用独立定价与支付接口。

当前阶段2-4提供项目导入、完整备份、编辑器试玩及前端网页游玩，阶段2-3已验收。剧情编排通过问问AI连接本人Codex，操作故事、时间线、按钮和QTE；素材入口采用总监对话、岗位交接和成果分区，平台文字固定DeepSeek Flash，图片使用商汤sensenova-u1.5-fast，视频使用北京wan3.0-video。连续视频使用n+1张边界图片和n段固定首尾帧视频，候选采用保留原裁切和互动。用户可在项目列表和固定版本记录中直接进入游玩页面，游戏ZIP保留为可选下载。本人确认没有旧作品；两个原工程实际导出的互通、导入映射、网页游玩及重启证明已保存。完整剧本到有序分镜的连贯制作按本人意见列入阶段3-1。

[真实Flash总画布工作流证明](.intermediate/agent-smoke/c25f0100-536e-4a90-b45c-bf7564c0e590/proof.json)：19次实际请求、供应商预估0.250127元、费用预留清零，完成视频裁切、普通按钮、QTE、场景正文及图片制作节点；运行程序核对三次跳转，实际服务重启后项目和任务一致。媒体使用既有真实视频，本次新增媒体模型请求为零。

## GitHub进度保存

代码、项目文档、设计文件和阶段验收记录保存在私有仓库https://github.com/Ethan3114/openfmv-workbench。仓库保留工具包目录结构，产品位于projects/OpenFMV互动游戏工作台/，根目录包含流程管家、验证脚本和对应手册。公开来源的设计参考图随设计文档保存。依赖、生成缓存、本机作品数据库、生成媒体及凭据保留在本机；在其他电脑恢复时需要安装依赖，并通过完整项目备份恢复创作数据。

当前完整剧情样例以《斗破苍穹》为题材，先查找已有剧情与媒体素材，再设计开头、中间剧情、选择点和两条后续线路，确认后细化每段视频制作清单。视频生成前的图文方案、实际图片及修正由代理自主判断并通过平台执行，视频生成前由本人核对首尾帧、镜头内容和参数并确认。

[素材来源](docs/剧情设计/斗破苍穹-素材来源.md)、[本机素材预览](docs/剧情设计/斗破苍穹-素材预览.html)及[剧情与视频制作清单](docs/剧情设计/斗破苍穹-剧情与视频制作清单.md)已保存。《戒中人》剧情与两条后续线路已确认，九个八秒剧情片段依次制作，每段按动作和对白拆成独立生成的子镜头。C01采用3秒、3秒、2秒方案进入平台确认，人物使用动画式正常身材比例。全部实际制作通过平台界面进行，记录位于[平台制作验收](docs/剧情设计/斗破苍穹-平台制作验收.md)。

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
backend/.venv/bin/python scripts/preview.py start --web-root "$PWD/.intermediate/frontend-web-play-r17-20261117"
```

停止本产品预览使用backend/.venv/bin/python scripts/preview.py stop。重复启动相同配置保持现有进程；更换独立构建目录前先停止当前预览。端口被其他进程占用时，启动命令会明确失败，维护者核实该进程后处理。

当前独立构建为frontend-web-play-r17-20261117，网页3210与API8310由预览管理程序检查。生成素材的岗位画布和媒体任务只显示当前一轮产物；旧素材继续保存在项目及手动画布中。新一轮模型读取范围限定为本次需求、本轮产物及明确选择的引用，历史对话仍完整保存。对话的“添加参考图片”可选择当前项目图片，最多五张，列表和已绑定图片均显示真实缩略图，可点击查看原图；真实文件归属、类型与摘要在服务端验证并绑定到当前任务。确认卡中补充图片需要填写修改要求，当前岗位重新制作后再次等待确认。素材库普通导入保存图片并保持面板打开。

素材交互测试为frontend/tests/browser/material-workspace.spec.ts，连续首尾帧及候选测试位于media-candidates.spec.ts，完整项目及前端网页游玩测试位于project-transfers.spec.ts。素材岗位每次执行一个函数，多操作响应按真实调用标识返回拒绝结果并有限重试；普通文字可按原文读取并标明来源类型。提交结果待核实的文字请求需要本人明确确认重试，原请求及预留继续计入原任务费用上限。scripts/recover_material_task.py提供管理员本人任务恢复及重启核验；retry-unknown必须提供原call-id、confirmed和max-steps，extend-limit在本人确认后提高请求次数，平台最高60次，费用上限保持。每次snapshot和record使用新的capture-id，原证明与登记记录保留。scripts/smoke_materials.py验证真实Flash岗位交接和引用改稿；scripts/smoke_project_transfers.py复用实际模型成片验证备份、固定版本及网页游玩。阶段是否表的正式六项结果由流程管家自检输出确定。

## 本机启动

项目导入位于“我的项目 → 导入与恢复完整项目”。支持OpenFMV的project.openfmv.json或project.json与实际assets组成的ZIP、无限画布version 3完整ZIP，以及本工作台备份。上传后检查场景、节点、媒体、来源映射及缺失项，确认后创建独立项目；相同文件和语义相同的同一版本重复导入均复用已有项目及已登记素材。原始完整来源与历史可通过所属导入记录的source-history接口读取。原任务作为来源保留，新的模型任务另行提交确认。

编辑器顶部“试玩”直接运行当前编辑内容，退出后继续编辑；“我的项目”中点击“在网页游玩”进入/zh-CN/play/<项目标识>，播放已保存的项目。编辑器“导出 → 项目备份与固定版本”提供完整备份和私有固定版本；完成记录中的“在网页游玩”进入/zh-CN/play/<项目标识>?version=<版本记录标识>，在前端直接操作普通按钮、QTE成功与超时分支，支持重新开始、刷新和返回编辑。固定版本的内容及文件摘要保持不变。登录归属检查在后端执行。游戏包下载保留在完成记录的展开项中。

完整备份包含项目、各场景画布、全部媒体、项目版本、对话、任务、工具、记忆、来源和候选；凭据、登录会话及平台设置排除。可选游戏包使用已有OpenFMV运行规则，包含index.html、game.json及播放媒体；创作依据和内部任务保留在完整备份中。

导入和导出记录持久保存，状态包含queued、processing、awaiting_confirmation、blocked、ready和failed。后台每项任务使用30秒执行租约，每5秒续期；服务中断后原租约到期可接续。缺失、变更和无法转换的内容阻止交付；目录修复后可明确重新生成原固定内容，旧文件和记录保留。媒体摘要按流读取，实际播放文件和ZIP均核对完整性。时间线保存和裁切保留毫秒精度。

相关API：POST `/api/v1/transfers/imports`（file与operationId），POST `/api/v1/projects/{project}/exports`（kind、expectedRevision、operationId），GET `/api/v1/transfers`，GET `/api/v1/transfers/{id}`，POST `/api/v1/transfers/{id}/confirm`（confirmationHash），POST `/api/v1/transfers/{id}/retry`，GET `/api/v1/transfers/{id}/download`，GET `/api/v1/transfers/{id}/play/index.html`及GET `/api/v1/transfers/{id}/source-history`。JSON结构位于backend/app/transfer_schemas.py；各接口在后端检查所属用户，错误使用error.code和error.message。

阶段2-4的机器证明保存在.intermediate/project-transfer-smoke-20261116/。scripts/smoke_project_transfers.py的prepare准备真实项目导出，deliver为本人账号保存独立验证工程，status读取状态，before和after比较实际服务重启前后内容，record核对原真实模型文件、完整项目及浏览器实际网页游玩，要求三个入口、QTE成功与超时、刷新、匿名访问拒绝及游玩零下载事件全部通过后生成记录。原供应商请求、调用时间和用量保留，工程验证不新增模型请求。本人确认没有旧作品；两个原工程实际完整导出的互通证明保存在.intermediate/native-interop-20261116/。

在终端进入本产品目录：

```bash
cd /Users/ethan/Documents/idea-to-launch/projects/OpenFMV互动游戏工作台
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
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8310
```

另一个终端窗口进入frontend并启动网页：

```bash
cd /Users/ethan/Documents/idea-to-launch/projects/OpenFMV互动游戏工作台/frontend
export TMPDIR="$PWD/../.intermediate/tmp"
export XDG_CACHE_HOME="$PWD/../.intermediate/cache"
npm run dev
```

注册与登录地址为http://127.0.0.1:3210/zh-CN/account，后端健康检查为http://127.0.0.1:8310/api/v1/health。

普通配置由OPENFMV_DATA_DIR、OPENFMV_FRONTEND_ORIGIN、OPENFMV_WORKER_ENABLED等环境变量提供。真实数据默认位于被Git忽略的data/；测试、缓存和下载素材位于被忽略的.intermediate/。所有依赖、命令和文件均在本产品目录内运行。

文本平台管理员由OPENFMV_TEXT_ADMIN_EMAIL指定。本机预览从被忽略的.intermediate/run/operator.json读取textAdminEmail并提供给后端；该文件只包含管理员邮箱。DeepSeek API Key通过管理员设置页面加密保存，普通注册用户无法读取或修改。销售计费策略未配置时，普通账号的收费文本请求返回明确错误，管理员开发验证受共享预算约束。

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

本人Codex连接使用本工程固定的官方CLI 0.168.0。在助手节点点击“创建配对”，双击产品目录的连接Codex.command，按终端提示确认默认本机API地址、粘贴配对标识与隐藏输入的配对码。配对后连接持续运行，网页显示“已连接”；关闭终端会停止本机连接，任务、工具结果及已提交媒体继续保存。配对信息和Codex登录凭据不发送到聊天。

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
export OPENFMV_REVISION_SNAPSHOT="$PWD/.intermediate/material-revision-recovery-20261118/failed-run.sqlite3"
export OPENFMV_REVISION_PATCH_SNAPSHOT="$PWD/.intermediate/material-revision-recovery-20261118/patch-limit-run.sqlite3"
export PYTHONPATH="$PWD/backend"
export TMPDIR="$PWD/.intermediate/tmp"
backend/.venv/bin/python -m pytest -q scripts/material_revision_validation.py --basetemp="$PWD/.intermediate/pytest-material-revision"
```

商汤使用原AI Studio的`sensenova-u1.5-fast`接口，三个账号各自并发1，额外请求持久排队。原配置通过下列命令加密导入，输出不包含凭据；导入操作不会调用模型：

```bash
backend/.venv/bin/python scripts/import_sensetime_accounts.py --source '../端侧AI-Studio/backend/.env' --email '1090444703@qq.com'
```

同步提交状态不明时保留原账号占用。未知请求不自动重发，不自动购买额度。商汤真实队列验证由`scripts/smoke_sensetime_queue.py`保存私有证明和自动图片冒烟记录；每次实际重试需要独立确认。`scripts/verify_image_restart.py`核对重启前后任务、文件摘要和账号占用。`scripts/smoke_material_review.py`使用真实DeepSeek Flash验证保存、暂停、通用继续接口拒绝及重启恢复，单次验证保留10元预算上限。

连续分镜使用R6工具目录及`schemaVersion=2`：n段视频对应n+1张独立边界帧。相邻视频共享同一图片素材，视频请求冻结图片标识和SHA-256，提交前核对原文件，再传入真实首尾帧数据。历史R1–R5调用及结构继续按原版本读取。

在工具包根目录运行：

```bash
cd /Users/ethan/Documents/idea-to-launch
bash kit.sh 自检 OpenFMV互动游戏工作台
```

自检输出保存到当前阶段证据目录。真实模型尚待验证时，该项保持待验；阶段通过需本人按验收清单实际核对并明确确认。
