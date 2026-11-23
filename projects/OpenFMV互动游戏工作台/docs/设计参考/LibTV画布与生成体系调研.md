# LibTV 画布与生成体系调研

最近核对日期为 2026 年 11 月 23 日。

## 原始卡片与生成器实测

原版截图以实际像素保存。SceneTurn 总稿的「04 手动素材画布」收录 17 张原图。画布总览为 25% 缩放，下面的几何使用 100% 缩放时测得的 CSS 像素。[Figma 工作流原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-8)

| 原版样本 | 作用与实测 |
| --- | --- |
| [文字卡片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-3) | 保存可引用的正文。本例主体为 350 × 200 像素。 |
| [图片卡片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-9)与[生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=16-3) | 展示一张媒体，可作为上游参考。本例卡体为 350 × 627 像素，标题行高 24 像素，图标 14 × 14，名称字号 13，规格字号 11。选中后生成器在卡下 16 像素，宽 660、高 174、圆角 20。 |
| [视频卡片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-6)与[生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=15-3) | 展示视频并提供素材连线。公开样本选中后，660 像素宽的生成浮层在卡体下方 16 像素居中。上方是引用缩略图和含有 `@` 引用的提示词，底部汇总模型、模式、画幅、清晰度、时长与数量。本例为 Seedance 2.0 VIP 的「全能参考」，在公开样本中只读。 |
| [音频卡片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-4) | 展示可播放的声音素材。实测可见表面为 320 × 146 像素，圆角 16 像素。 |
| [脚本卡片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-5) | 用分镜表展示可引用的剧本内容。本例为 800 × 400 像素。 |
| [分组](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-11) | 包住相关素材与卡片，名称放在分组区域上方。 |
| [连线布局](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-10)与[悬停](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-12) | 锚点在卡片左右边缘的垂直中点。静止时不呈现常驻圆点。默认曲线为 `#86909C`、2 像素，透明命中宽 20 像素。前向曲线的两个控制点横坐标为两端横坐标的中点。悬停时基线改为 `#C0C8D0`、4 像素，过渡 200 毫秒，并叠加移动的蓝色虚线。 |
| [缩放](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-2)与[故事板](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-7) | 工作流与故事板切换位于画布上方。缩放等画布控制位于左下方。 |

点选公开画布上的已有卡片，可以读取该卡保存的生成设置。[LibTV 原始公开工作流](https://www.liblib.tv/canvas/share?shareId=8h9jw9muu) · [含脚本和音频的公开画布](https://www.liblib.tv/canvas/share?shareId=BGmI2IfM5)

## 可编辑画布实测

同一 Figma 页面另外收录了 17 张登录态截图，裁去浏览器外框后每张保留 2474 × 1694 个原始像素。Figma 接收的文件摘要与本机裁切结果一致。[可编辑画布与连线](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-15)

| 状态 | 原版操作 |
| --- | --- |
| [添加节点](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-16) | 文本、脚本、图片、视频、导演台、智能剪辑、音频依次排列。添加资源分组提供上传与从生成历史选择。脚本展开新版和旧版。 |
| [Wan 3.0 模式](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-5) | 文生视频、全能参考、首尾帧、视频续写。在空节点上，后三项置灰。选择模型后，模式按钮旁汇总画幅、清晰度、时长和个数。 |
| [Seedance 2.5 Flash 模式](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-3) | 文生视频、全能参考、图生视频、首尾帧、图片参考、视频编辑、视频续写、超长视频 Beta 按顺序列出，缺少素材时相关项置灰。 |
| [首尾帧预设](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-11) | 点击卡片预设后进入首尾帧模式，生成器上方分别显示首帧、尾帧插槽。预设实例使用两个相连的示例图片节点。 |
| [生成图片](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-14) | 提示词位于上方，底边使用模型和合并后的规格控件。本例为 16:9、标准画质、2K、1 张。 |
| [脚本生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-12)与[镜头表](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-2) | 卡片可按剧本、角色生成分镜，或手工录入镜头。镜头完整表格提供时长、镜头和画面描述。 |
| [音频生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-17) | 标签依次为配音旁白、音乐配乐、音效、音色生成。配音提供音色、表现力、语言、模型和输出规格。 |
| [导演台](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-8)与[智能剪辑](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-7) | 导演台打开具有导演视角、机位视角和三轴控制的独立 3D 创作界面。智能剪辑接受视频节点，空卡展示素材混剪入口。 |
| 连线端点 | 编辑状态的端点在可操作时显示「拖拽连线或点击选择节点类型」。多选实际图片与视频后的下游菜单依次显示文本生成、图片生成、视频生成、智能剪辑、音频和脚本生成。可用状态取决于上游素材的组合。 |

导演台的 3D 界面已核对角色、姿势、属性和三轴变换控件。LibTV 指南记录了人体素模、基础几何、群众阵列和本地模型四种输入，以及机位视角截图、将截图送回画布作为参考图的流程。官方 Blender 插件另提供白模视频导出到 LibTV 的路径。可导入的文件格式、场景恢复和导出规格仍需在编辑器完成实测。SceneTurn 可复用现有画布版本与项目素材存储，3D 渲染、模型导入和机位操作待实现。[LibTV 使用指南第 2.4 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97) · [LibTV × Blender](https://www.liblib.tv/blender)

音频目前使用现有项目媒体流程，配音、配乐、音效和音色生成需要各自的模型请求、任务状态和结果文件。

已保存的 Wan 视频编辑任务继续使用其原始执行参数。新建视频节点的可选项按模型能力与实际已连接的素材提供，模型提交仍保留工作台的核对步骤。

## 节点点选及后续操作

用户提供的「预设 - 首尾帧生成视频」截图显示，选中已有两张参考图的视频节点后，整套生成器出现在卡片下方。上排显示「风格、配色、构图、节奏、相机」，编辑区上方显示「上传、画布参考、标记、预设提示词」。两个引用缩略图标明「首帧」「尾帧」，已有提示词显示在其下方并附总字数。底部依次显示 Seedance 2.5 Flash、「首尾帧」、「Auto / 720P / 5s / 1个」、声音、图标工具及 140 积分按钮。截图只证明控件在这个节点状态中出现。各控件打开后的内容、选择时修改的字段和关闭后的状态需要在原版实测。

同一套首尾帧预设已在登录编辑画布中重新打开。Figma 新增了完整选中态与点击「风格」后的原图；通过 Figma 返回的图片摘要核对了上传内容。[首尾帧完整面板](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [影视风格弹层](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-2)

按正常创作顺序核对每个模块。每项保存卡片选中后的完整画面、展开菜单的画面，以及选择或取消后的结果。对含素材的节点另核对预览、替换素材、重新打开和拖线。模型调用以提交前的可核对状态作为本次采集终点。

| 模块 | 已有画面 | 需要逐项点击并记录的内容 |
| --- | --- | --- |
| 文本 | 登录态空卡和快捷入口 | 自己编写内容、文本生成模型、图反推选材、文生视频和文字生音乐各自打开的界面；已填内容的工具条、保存和重选。 |
| 脚本 | 新旧版菜单、默认生成器和手编空表 | 剧本入口和角色入口各自的素材选择及参数；实际填入镜头后的行编辑、参考图、批量操作和输出节点。 |
| 图片 | 空图片节点与默认生成器 | 已有图片的选中工具条；图生图、图片高清、影视指导、模型、画幅、画质、分辨率、张数和选材弹层。 |
| 视频 | 默认面板、两种模型的模式菜单和首尾帧预设启动状态 | 给首尾帧装载实际素材后，逐一打开影视指导五项、上传、画布参考、标记、预设提示词、提示词右上工具、模型、模式、规格、声音和高级设置。核对换模型、换输入模式、换帧及切换节点后的保留状态。 |
| 音频 | 默认配音生成器和四项标签 | 分别进入配音旁白、音乐配乐、音效、音色生成；记录各自的素材、模型、音色、语言、表现力、输出规格和预览入口。 |
| 导演台 | 卡片入口与 3D 主界面 | 角色、姿势、属性、相机、导入和截图回填；记录画面变化与输出参考图所在位置。 |
| 智能剪辑 | 卡片和素材混剪入口 | 展开混剪后的选材、顺序、裁切、声音、预览和导出界面；记录有无视频输入时的变化。 |

SceneTurn 的生成器目前已提供首尾帧与参考素材、可编辑的提示词、模型、模式、画幅、清晰度、时长和生成前核对。视频截图中的影视指导、面板内上传、标记、预设提示词、声音和高级设置仍有差异。普通文本、已有图片和音频点选也需要核对各自工具条。[当前的生成面板](../../frontend/app/_features/creation-canvas/upstream/canvas-config-node-panel.tsx) · [当前的节点点选分流](../../frontend/app/_components/workbench/StudioPage.tsx)

## 公开前端功能清单

本机归档目录为 `.intermediate/libtv-public-component-audit-20261123/`。保存了公开画布页面、268 个 JS 文件和 31 个 CSS 文件。其中 167 个 JS 文件由页面直接引用，101 个由代码在运行时加载。实际账号开放的模型、素材库内容、积分和后端生成结果以原版实际界面为准。下列项目可分别决定是否保留。

| 模块 | 可以分别选择的功能 | 操作与结果 |
| --- | --- | --- |
| 通用画布 | 工作流及故事板、按节点添加与拖线添加、分组、缩放、小地图、隐藏连线、网格吸附、资产管理、生成历史 | 点击卡片打开对应的编辑区域。拖线可进入下游新建菜单，符合当前节点和模型规则的素材才可连接或引用。[节点菜单代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1j8f6qw00r6_e.js) |
| 文字 | 直接写、文生视频、图片反推提示词、文字生音乐、模型生成、翻译、引用编排 | 「自己编写内容」使空卡原地切到输入框。文生视频和文字生音乐创建下游节点、自动连线并移动焦点；图片反推先在左侧创建示例图并接回文字节点。独立文本生成器按模型接受和排序已连接的文字、图片、视频和音频。[空卡事件代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1tjx0-9ctxmqp.js) · [文字生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/11o0kaqhh0zef.js) |
| 脚本 | 新版与旧版、剧本生成分镜、角色生成分镜、自己编写、镜头表、素材引用、按镜头继续生成 | 剧本入口创建并连接上游文本；角色入口连接两张示例角色图并写入提示词；手编打开分镜表并创建首个镜头。新版添加参考支持画布、本地和个人素材库，可排序、移除或插入提示词。指南记录了分镜图、视频读取本镜剧本和图像的流程。[空卡事件代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1tjx0-9ctxmqp.js) · [脚本生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/3is9nsyn596-x.js) · [LibTV 使用指南](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97) |
| 图片 | 图生图、高清、风格、配色、构图、相机、模型与规格、参考图、预设提示词、结果图编辑 | 空卡的图生图打开选图，高清使用示例图新建增强节点。带图节点还有裁切、扩图、局部编辑、去除元素、抠图、打光及机位相关功能，具体展示取决于节点和模型。图片规格组合为画幅、画质、分辨率与张数。[空卡事件代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1tjx0-9ctxmqp.js) · [LibTV 使用指南图像工具](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97) |
| 视频 · 影视指导 | 风格、配色、构图、节奏、相机 | 区域可折叠。风格、配色、构图与节奏经选择器确认后保存到节点，已选项目有名称或预览，可清除。节奏为视频专用，可预览示例。相机单独打开机型、镜头、焦距、光圈四列，确认才应用；默认各项为 Auto。[公共生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/03uiohkeja4lu.js) · [视频风格实测](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-2) |
| 视频 · 素材和提示词 | 上传、画布参考、标记、预设提示词、引用缩略图、`@`、提示词编辑和字数 | 上传菜单分本地与已有素材。本地文件创建素材节点并与目标关联。「画布参考」进入选材模式，再点可退出。「标记」根据模型 `magic` 能力显示和切换画布标记模式。「预设提示词」打开官方库、个人库、搜索、查看与添加入口。带首尾帧的缩略图独立标示用途，字数包含输入框和所接文字。[公共生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/03uiohkeja4lu.js) · [视频生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/3crez0-u4yl4i.js) · [首尾帧原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) |
| 视频 · 输出控制 | 模型、输入模式、比例、清晰度、时长、数量、声音、分镜提示、翻译、运镜预设、效果、主体、高级设置、生成 | 公开代码区分纯文字、单图、首尾帧、图片参考、视频参考或编辑、音频驱动和混合参考，并按当前输入校验。模型、模式和素材数量共同决定可用入口。当前首尾帧预设可见联网搜索、自动校验素材、智能引用 AutoLink；先前 Wan 3.0 画面另可见智能扩写。[视频生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/3crez0-u4yl4i.js) · [Seedance 模式实测](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-3) · [Wan 模式实测](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-5) |
| 音频 | 配音旁白、音乐配乐、音效、音色生成、语言、音色、表现力、试听、模型与输出规格 | 四种模式有各自的草稿和筛选条件，切换时可保留编辑。配音、音乐和音效各自显示相关参数和高级设置。音频卡的播放与生成器分开操作。[音频生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/23buf4igm98oi.js) · [默认配音面板](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-17) |
| 智能剪辑 | 素材混剪、讲解视频、批量广告等模式、素材编排、字幕、音轨、裁切、预览、文件或画布导出 | 前端分别管理视频源、音频源、片段和字幕；时间轴提供播放头、片段裁切边缘和音量线。导出可设置尺寸、帧率、格式和分辨率。独立「视频合成」节点另支持连接多个视频或视频加音频后打开时间轴。[剪辑代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/07nqyl8nponps.js) · [导出代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1_4-90dnbtn32.js) |
| 导演台 | 对象、人体姿势、场景背景、机位、视角、关键帧、截图回填、白模视频 | 可从卡片进入独立 3D 空间。普通模型代码接受 GLB、GLTF、OBJ、FBX、STL、DAE，高斯场景另有 PLY、SPLAT、SPZ、KSPLAT。时间轴可为摄像机、人物、道具及分组设关键帧，可调整视野、注视目标、姿势和轨迹曲线。机位快照送回图片节点，动画以 30 或 60 FPS 无声 MP4 送回视频节点。完整的导入和导出操作仍待实测补图。[导演台原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-8) · [导演台界面代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/3nu_ic217gy39.js) · [LibTV × Blender](https://www.liblib.tv/blender) |

图片和视频共享部分生成器控件，各自的操作状态由独立组件维护。图片的相机选项要求模型提供 `cameraControl`，焦点编辑可选画布区域并返回当前目标，节奏为视频专用。视频的影视指导与 3D 导演台分别使用各自的组件与数据。[图片生成器代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/2c4qtlc_w29si.js) · [生成器加载清单](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/0w5igox59bcfd.js)

点选由节点类型和当前状态决定打开哪个界面。合适的节点单选后显示下方生成器，普通框选和拖动时收起。选了历史版本时，位置相同的区域显示版本信息。画布参考、标记和焦点编辑各自保留目标节点。媒体卡片的选中态以不同边框颜色标示。图片增强、视频增强、去字幕和音频编辑有独立分支。3D 导演台通过卡片里的按钮进入工作区。卡片上的按钮、菜单和编辑器也各自处理点击或双击。[节点界面分派](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/1tjx0-9ctxmqp.js) · [画布点选规则](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/24cywatvj0-3x.js)

## 核对范围

- 在公开分享画布中读取了实际节点和连线。该示例有一张图片连到三个视频节点，每个节点左侧是输入端口，右侧是输出端口。节点显示素材像素尺寸，画布提供「工作流」「故事板」、小地图、缩放和连线可见性切换。分享状态提供查看和复制项目的入口。[LibTV 公开工作流](https://www.liblib.tv/canvas/share?shareId=8h9jw9muu)
- 官网有视频、图片、音频、剧本和智能剪辑的独立入口。未登录点击「视频生成」会出现登录窗口。下文的创作功能来自可读的《LibTV 使用指南》转载，画布截图和几何则来自前文列出的原版实测。官网插件页的飞书指南要求登录。[LibTV 首页](https://www.liblib.tv/) · [LibTV 插件页](https://www.liblib.tv/plugin) · [《LibTV 使用指南》转载](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)
- 火山方舟文档用于核对模型的真实输入方式和约束。文档中的 API 上限属于厂商规格，LibTV 平台内的上传限制和全部下拉选项需要登录验证。[火山方舟视频生成 API](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)

## 画布和连线

指南把文本、图片、视频、音频和脚本列为五种基础节点。双击空白区域创建节点，也可以将图片、视频或音频从本地拖进画布。拖动右侧输出端口到空白区域可创建下游节点，引用素材随连线传入生成器。比如将风格图接到图片节点，再接一个角色设定文本，生成器会同时收到风格参考和文本内容。同一份输出可以被多条连线使用，实际公开画布里的图片节点就同时给三个视频节点提供参考。[公开画布](https://www.liblib.tv/canvas/share?shareId=8h9jw9muu) · [《LibTV 使用指南》转载第 1.2、1.3 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

结果回填到生成节点。节点菜单提供删除、创建资产和复制。指南区分「创建副本」和普通复制粘贴，前者会继承已有连线，便于在原参考下调整提示词，生成另一个版本。节点可组成可复用工作流。生成器里支持以素材名称引用输入，Seedance 2.0 的文档例子用 `@图片1`、`@视频1`、`@音频1` 指定用途。[《LibTV 使用指南》转载第 1.2、1.3、5.2 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

## 文字与剧本

- 文本节点可直接写入和粘贴，也可以在节点生成器选语言模型、编辑自然语言需求或结构化提示词，结果显示在节点中。指南列举了剧情扩写、角色设定、提示词整理，以及使用支持多模态的模型分析图片和视频。
- 脚本节点有单独的分镜表或卡片视图。可以用剧情描述、整份剧本、角色参考图或参考视频创建脚本，双击表格单元格修改内容、替换参考图。用户能勾选一个或多个分镜生成图片，选风格、模型、画幅、分辨率，再从分镜生成视频。批量转视频时，指南明确说明视频根据脚本和图像生成，同时提供模型与时长设置。[《LibTV 使用指南》转载第 1.2、5.3 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

## 图像生成与编辑

- 图片节点可由上传或生成得到。生成器允许直接编辑提示词、调整选中的生图模型、画幅和张数，分镜图片生成也可选分辨率。指南列出的输入包括纯文本、单图或多图参考，输出包括新图片、多图融合和指令编辑。具体分辨率、参考数量和画幅由模型决定，指南中的 LibNavo Pro 和 Seedream 4.0 支持 2K、4K。当前官网主推 Seedream 5.5。[LibTV 首页](https://www.liblib.tv/) · [《LibTV 使用指南》转载第 1.2、1.3、5.1 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)
- 选中图片后，节点的工具栏可以针对当前图像裁剪、高清放大、扩图、局部重绘、擦除和抠图，也可生成其他机位或重新打光。网格图可以切成单独分镜节点。指南还记录了九宫格机位、角色三视图、剧情推演等以参考图为输入的快捷任务。[《LibTV 使用指南》转载第 2.1、2.2、3 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

## 视频生成、参考、编辑与延长

视频节点支持上传本地视频与模型生成。LibTV 的官方插件文档也明确描述了先上传视频再编辑其元素、调整镜头、迁移风格、以视频为参考创作，以及视频续写。官网视频页列出 Seedance 2.5、Seedance 2.0、MiniMax H3、Kling O3、Happy Horse 1.1。具体的模型能力需要按模型版本核对。[LibTV 视频产品页](https://www.liblib.tv/lp/ai-video) · [LibTV 发布的 Agent 技能](https://github.com/libtv-labs/libtv-skills/blob/main/skills/libtv-skill/SKILL.md)

| 用法 | 输入及控制 | 输出 |
| --- | --- | --- |
| 文生视频 | 编辑该镜头的描述，根据模型设置画幅、分辨率和时长 | 根据镜头描述生成视频 |
| 首帧或严格首尾帧 | 为首帧和尾帧分别绑定图片。镜头文本用于描述动作、运镜、对白及时间安排。严格首尾帧与全模态参考是互斥的任务模式 | 以指定的首帧或首尾帧组织镜头衔接，画幅根据受支持的模型规则决定 |
| 参考视频生成 | 连接源视频，可以另外引用图片、音频。用提示词明确每个素材提供的动势、运镜、主体、风格或配乐 | 参照源视频的指定信息生成新画面，可选输出规格依模型而变 |
| 视频编辑 | 指定源视频和要修改的画面、声音或时间范围，可附替换素材。Seedance 2.5 继承来源画幅和时长 | 源视频的修改版本 |
| 视频延长 | 指定源视频，写明向前或向后继续的内容。Seedance 2.5 继承来源画幅。续写总长按接口和模型校验 | 延续源视频前后内容的画面 |

LibTV 指南的 Seedance 2.0 示例把「首尾帧」和「全能参考」分为两个入口。「全能参考」中可以用提示词描述某张参考图的首尾位置，严格帧控制使用专门的首尾帧入口。指南还记录了视频生成器的运镜预设及自定义运镜提示词，Kling O1 和 O3 可在相应生成器中引用由多张图或一段视频建立的主体。[《LibTV 使用指南》转载第 4、5.2 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97) · [火山方舟 Seedance 2.5 提示词指南](https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=zh)

### Seedance 厂商限制

| 方舟模型 | 参考图 / 视频 / 音频上限 | 参考视频时长 |
| --- | --- | --- |
| Seedance 2.5 | 30 张 / 10 段 / 10 段 | 单段 2–30 秒，编辑任务单段 4–30 秒，输入视频合计最多 30 秒 |
| Seedance 2.5 Flash | 10 张 / 5 段 / 5 段 | 同 2.5 系列限制，输出分辨率为 480p 或 720p |
| Seedance 2.0 系列 | 9 张 / 3 段 / 3 段 | 单段和合计最多 15 秒，单段至少 2 秒。使用音频时还需要图片或视频。LibTV 指南另列单次参考文件合计 12 个 |

方舟参考视频可通过符合要求的 URL 或素材 ID 传入，接受 MP4、MOV，单个文件不超过 200 MB，支持 24–60 FPS 与指定编码。厂商对真人脸素材另有校验与授权要求。接入时须按供应商、模型和任务类型校验格式、数量、分辨率、时长与输入角色。[火山方舟视频生成 API](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh) · [火山方舟视频生成教程](https://docs.volcengine.com/docs/ark/video-generation-tutorial?lang=zh) · [方舟模型发布公告](https://docs.volcengine.com/docs/ark/model-release-announcement?lang=zh) · [《LibTV 使用指南》转载第 5.2 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

## 音频与剪辑

- 音频节点可拖入已录制的音频。指南描述了文本转语音、配乐和音效的不同生成方式。配音支持按模型选音色、设置语速和音高。音乐和音效可单独作为素材接到支持多模态输入的视频节点。[《LibTV 使用指南》转载第 1.2、5.4 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)
- 视频节点可以裁取片段，设置时间轴入点和出点，也可以分析镜头、导出音频或分离人声与背景声。把多个视频或视频与音频一起连到「视频合成」，会得到带时间轴的合成节点。指南记录了片段排序、裁剪、预览、独立音轨混音和成片下载。此处属于确定性剪辑，多模态模型的画面重拍和续写属于生成任务。[《LibTV 使用指南》转载第 2.3 节](https://www.scribd.com/document/1063078487/LibTV%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97)

## SceneTurn 对应需求

| 位置 | 需求 |
| --- | --- |
| 问问 AI | 沿用会话历史、总监分工、岗位产物与人工确认。右侧对话可引用选中的可见产物，跟进同一任务。制作安排和开发人员使用的结构化字段留在内部 |
| 手动素材画布 | 画布中使用独立的节点控件，创建或导入素材、拖动连线、管理已连接的引用。每个生成节点内直接修改提示词和选择模型，参数与已接通的模型能力一致。复用上游素材时保留来源关系，删除连线后及时更新引用 |
| 文字和分镜 | 文本节点有直接编辑入口与模型生成入口。分镜引用本镜剧本，并把该镜的动作、对白或音效传入视频节点。连续 N 段严格首尾帧用 N+1 张边界图片，相邻镜头引用同一个边界素材 ID |
| 图像 | 画幅与清晰度分为两个控件，展示实际可用规格与数量。确认后保存选定模型和素材引用。已有个人 Codex 图片生成能力可在同一模型选择入口提供 |
| 视频生成 | 分清严格首帧、严格首尾帧、参考视频生成、视频编辑与视频延长。首尾帧与全参考互斥。编辑与延长明确选中源片段，按能力锁定参数。确认卡保存当时选定的剧本、提示词、帧、参考视频和生成参数。变更输入后应重新确认，既有成功产物保留 |
| 音频和剪辑 | 可在素材画布连接或查看音轨，导入时间轴后由创作者或本人 Codex 完成排布、裁剪和互动配置。基于生成模型的配音、音效或视频重拍沿用素材生成任务及状态 |
| 素材保存与隐私 | 源文件与生成产物留在项目私有存储，先校验归属，再按任务向对应厂商提供最少的参考素材。若接口需要 URL，应使用限时授权的地址，避免将链接写入对话和日志。方舟返回的视频下载地址有效期为 24 小时，完成后需要下载入库，供项目持续预览和复用。创作者需有权提交含真人的参考素材 |
| 费用和供应商回执 | 保留逐次视频确认，记录内部报价依据。方舟 Seedance 2.x 按是否含参考视频采用不同单价，估算用量含输入视频时长和输出时长，并可能受最低用量约束。持久化供应商任务状态、输出链接入库结果、真实用量和费用预留，避免提交结果待核实时重复扣费 |

厂商链接有效期和计费口径依据公开接口说明。[火山方舟查询视频任务](https://docs.volcengine.com/docs/ark/get-video-generation-task-api?lang=zh) · [火山方舟模型计费](https://docs.volcengine.com/docs/ark/model-pricing?lang=zh)

## SceneTurn 手动画布

入口为「素材 → 生成素材 → 手动编辑画布」。可以把节点右侧输出端拖到已有节点，也可以拖到空白处打开下游创建菜单。菜单根据素材类型、所选模型和任务用途列出能接收该素材的操作。选中连线可断开。节点内直接编辑提示词，键入 `@` 可点名已连接的素材。新增输入或修改参数后，确认卡会按新输入重新准备。已保存的素材仍可用于其他节点。

| 生成体系 | 节点中可设置的内容 | 结果与限制 |
| --- | --- | --- |
| 文字 | DeepSeek Flash 或 Qwen Flash，剧本创作、摘要、角色画像、道具、场景、分镜情节、单镜提示词；可合并引用已连接的文字 | 生成正文保存到文字结果节点。项目仍可使用原有的素材对话来安排多个岗位 |
| 图片 | 商汤 Fast、Seedream 5.5 或个人 Codex；角色、道具、场景、分镜；16:9、9:16、1:1、4:3、3:4、3:2、2:3。像素尺寸依据模型限制显示。可以连接多张项目图片，并编辑给模型的完整描述 | 每次一张，确认卡显示型号和实际像素，生成后回填图片节点并登记素材。节点可调用现有图片切片，切片保留独立素材 |
| 文字生视频 | 支持的万相、Seedance 型号，提示词、分辨率、时长、画幅 | 单次一段，实际提交前核对当前镜头 |
| 首帧与首尾帧 | 从连入的项目图片选择首帧，支持尾帧的型号可再指定尾帧；可同时连接本镜剧本。部分型号要求自适应画幅 | 视频方案展示带名称的实际原图和完整镜头文字；由 N 段文字展开时，建立 N+1 个帧节点，相邻段共用边界 |
| 参考素材生成 | 万相 3.0，可从连线选用一段项目 MP4、最多十张参考图，或同时选用两者。每张图可单独勾选，提示词中可用 `@` 点名图与视频 | 可仅用一张或多张参考图。参考图按选择顺序编号；确认卡展示实际提示词、连线接入的镜头文字、素材和生成参数 |
| 视频编辑与延长 | 万相 3.0，选用一段项目 MP4，同时可选最多十张参考图。用提示词指定替换对象、需要保留的镜头运动、修改范围或延长方向 | 原视频源可以是上传的 MP4 或项目里已保存的成片。参考视频 1–15 秒，输入和输出合计最多 30 秒。提交前再次校验文件与确认内容 |
| 音频与剪辑 | 导入音频可供画布预览及加入时间线。连接两段以上视频可按连线顺序在本机拼接 | 音频参与视频模型生成、配音、音乐和音效模型尚未接入手动画布。剪辑、编排和互动可继续在故事编辑器操作 |

先连接文字、图片和视频，再在当前视频节点选择素材用途。切换生成方式保留连线；文字模式使用文字，固定帧模式使用指定的首帧与尾帧，编辑与续写使用选中的视频与参考图。已存在的 `@` 引用保留，改模式后若引用了本次未选用的素材，节点会提示调整。文字节点已连入时，即使提示词使用了 `@` 指定图片，也会附上该段文字。

创作者确认后，后端把项目内的原视频上传到百炼的私有临时存储，临时地址有效期为 48 小时。确认前只核对本地文件与内部额度。万相混合参考的实际上传、出片和供应商账单仍待验证。Seedance 2.x 的软参考已在官方接口公布，仍需完成项目私有视频传输与输入、输出分别计量的费用校验。[万相 3.0 API](https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference) · [百炼临时文件上传](https://help.aliyun.com/zh/model-studio/get-temporary-file-url) · [火山方舟视频生成 API](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)

## 创作者公开画布

2026-11-22 核对九份公开制作画布。以下连接关系来自浏览器加载后的实际节点和连线。

| 公开画布 | 可核对的用法 |
| --- | --- |
| [鱼否《吃顿饺子，怎么这么难》](https://www.liblib.tv/creation/process?id=da9d73940504423fa168934fbcff72f0) | 第一镜收到三条角色声音、七张角色或道具或场景图和一份分镜文字。同一成片还可以接高清处理，画布保留人物方位修改的失败分支 |
| [Rick《长安异闻录·惊夜》](https://www.liblib.tv/creation/process?id=386cfd274d084d2ba9add9dc8480427e) | 以场次分组。旧视频可派生片段重拍、剪辑和高清正片，旧视频的尾帧和时间戳截图也保留为素材 |
| [烟墨鱼《从0开始的LibTV生活》](https://www.liblib.tv/canvas/share?shareId=BGmI2IfM5) | 脚本接分镜图组，分镜组接视频组；角色三视图、战斗说明、场景图、拼接分镜和声音可以汇入一个视频节点 |
| [《钽星·高塔之影》](https://www.liblib.tv/canvas/share?shareId=KPDarrNh6)、[《零界：深渊裂变》](https://www.liblib.tv/canvas/share?shareId=UkIjF494o) | 角色与武器等设定独立保存，可用于各镜分镜。不同镜头的剧本、生图和视频要求分别维护 |
| [一图派生三个视频](https://www.liblib.tv/canvas/share?shareId=8h9jw9muu) | 一张图可同时连接多个视频节点，各视频可以采用自己的提示词和参数 |

节点样式使用经授权的 LibTV 公开卡片样式。媒体卡片采用深灰表面、16 像素圆角和细边框，名称与规格放在卡片上方同一行，左右连接端口位于卡外，并按不同缩放级别保留足够的拖动区域。画板覆盖视频编辑、参考生成、视频续写和最终核对。[Figma 手动素材画布](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=0-5)
