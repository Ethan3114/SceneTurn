# LibTV 素材画布模块评审

审阅范围为桌面端素材画布，核对日期为 2026 年 11 月 23 日。下表的编号供逐项批注。**「原图」**表示 Figma 中有对应的原版画面，**「实测」**表示在原产品中点开核对，**「代码」**表示公开前端包含所述操作，**「指南」**表示原产品指南描述了该流程。原图记录的是当时的模型、素材和选中状态。各角色、模型最终能否成功生成，需要单独核验。功能取舍由产品负责人填写。[Figma 素材画布](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=0-5)

## Figma 评审入口

[评审目录](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=46-2) · [43 项卡片与连线功能](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=32-2) · [12 项外围入口](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=41-2)

| 审阅对象 | 画板 |
| --- | --- |
| 卡片、输入端、输出端 | [五种卡片的默认与选中状态](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=33-2) · [连线与端点](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=36-2) |
| 节点内的生成面板 | [带引用的文本生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=35-2) · [首尾帧视频、图片规格及四种音频方式](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=38-2) |
| 独立制作界面 | [3D 导演台和智能剪辑](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=45-2) · [原版手工镜头表](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-2) |
| 原版连续操作画面 | [工作流、脚本、图片等 24 张](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=42-2) · [视频、声音 21 张](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=39-2) |

核对细小控件时可以打开原尺寸的[图片输出规格](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=40-5)、[相机参数](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=40-6)、[预设提示词库](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=44-3)。导演台里的三维视窗保留为静态预览，周边机位、角色和属性控件单独成层。新版脚本的生成入口与手工镜头表有原版截图，对该版本各行编辑、取消、重新打开的过程仍需逐项实测。

## A　画布、选中和连线

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| A01 | 新建节点 | 打开添加菜单，依次可选文本、脚本、图片、视频、导演台、智能剪辑、音频。脚本展开新版和旧版。下方的资源入口可以上传文件或从生成历史选择。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-16) |
| A02 | 编辑一个节点 | 单击可编辑的生成节点，选中边框发生变化，在卡片下方出现该类型的编辑面板。拖动卡片时收起面板，结束拖动后根据选中状态恢复。框选期间收起普通节点面板。查看历史版本时，同一区域可显示版本信息。上传的资源卡、生成卡、处理结果有各自的显示条件。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| A03 | 连接已有素材 | 从上游卡片右侧端点拖到目标输入端。输出可以分给多个下游，目标节点的生成面板用缩略图或文字显示所接素材。点击「画布参考」时保留正在编辑的目标，允许用户在别处选素材，再点入口可退出选材。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-15) · [代码](../../.intermediate/libtv-public-component-audit-20261123/03uiohkeja4lu.formatted.js) |
| A04 | 从素材继续制作 | 把输出端拖向空白处或点击可操作的端点，打开下游菜单。多选图片和视频的实测菜单提供文本生成、图片生成、视频生成、智能剪辑、音频和脚本生成。可用状态随上游组合变化。选中可用项后创建节点并接入输入。 | [原图所在页](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=0-5) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1j8f6qw00r6_e.formatted.js) |
| A05 | 看清素材之间的关系 | 端点位于卡片左右两条边的中点。空闲时圆点收起，可操作时显示端点。100% 缩放下，常态曲线宽 2 像素，透明命中区域宽 20 像素。悬停时曲线加粗到 4 像素，并显示流动的蓝色虚线。 | [连线原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-10) · [悬停原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-12) |
| A06 | 移动视野和整理卡片 | 左下缩放菜单可输入百分比、放大、缩小、适配画面，或切换 50%、100%、200%。同一菜单提供小地图和网格吸附。分组以命名区域包住相关卡片，画布另有连线可见性开关。 | [缩放原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-2) · [分组原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-11) · [代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| A07 | 复用节点和比较版本 | 节点菜单提供删除、创建资产、复制。指南区分普通复制粘贴与「创建副本」，后者继承连线，可在同一组参考下试不同提示词。历史条目提供独立查看状态。删除后的下游表现、版本切换后的重选仍需操作核对。 | [历史面板代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) · [指南出处](LibTV画布与生成体系调研.md#画布和连线) |
| A08 | 查看故事板或打开创作对话 | 顶部可切换工作流和故事板。故事板的布局菜单支持将对话放在左侧或右侧、显示或收起素材。对话欢迎区可按已有节点显示「继续」类入口，也有原创剧本、故事改编、分镜入口，分别打开设置、选取源文件或预填对话。实际角色分派和会话恢复待在账号中核对。 | [故事板原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-7) · [代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |

## B　文本

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| B01 | 自己写一段文字 | 在空文本卡点「自己编写内容」，卡片原地出现可编辑的正文。该节点可以向下游提供剧本、对白或提示词。 | [文字原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-3) · [空卡代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| B02 | 用模型生成文字 | 在文本生成面板填写要求、选模型。模型能力决定文字、图片、视频、音频引用是否可用。可调整参考顺序、执行提示词翻译并提交，结果供文字节点展示。只读画布限制编辑与提交。 | [代码](../../.intermediate/libtv-public-component-audit-20261123/11o0kaqhh0zef.formatted.js) |
| B03 | 从文字出发生成视频或音乐 | 分别点击空卡的「文生视频」「文字生音乐」，系统创建相应的下游节点、自动连线并切换焦点。画布上保留原文字来源。 | [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| B04 | 从图片得到提示词 | 点击「图片反推提示词」，示例图片出现在文本卡左侧，并作为其输入。应审阅图像来源、文字指令及结果文字三个位置。 | [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |

## C　脚本和分镜表

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| C01 | 从剧本或角色开始拆分镜头 | 「剧本生成分镜」建立上游文字节点并连入脚本。「角色生成分镜」接入两张示例角色图并写入提示词。两条路径进入分镜制作时带入的资料不同。 | [生成器原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-12) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| C02 | 手写镜头 | 「自己编写」打开镜头表并建立第一行。表格展示时长、镜头、画面描述。指南说明可双击单元格编辑、替换参考图。已填表格的关闭、重新打开和修改保留情况待核对。 | [镜头表原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-2) · [指南出处](LibTV画布与生成体系调研.md#文字与剧本) |
| C03 | 给新版脚本添加参考 | 点击「添加参考」，选择画布、本地、个人素材库。本地支持多选 PNG、JPEG、WebP、GIF，逐张生成图片节点并与脚本关联。素材库可以取消或确认选择。脚本内的引用可排序、移除，并在提示词里点名。 | [代码](../../.intermediate/libtv-public-component-audit-20261123/3is9nsyn596-x.formatted.js) |
| C04 | 逐镜生成分镜图和视频 | 指南描述可勾选一行或多行分镜，选择风格、模型、画幅、分辨率出图。分镜转视频时读取该镜剧本与图像，并可选模型、时长。每次提交的实际参数与生成结果待核验。 | [指南出处](LibTV画布与生成体系调研.md#文字与剧本) |
| C05 | 打开旧版脚本 | 添加菜单区分新旧脚本，前端加载不同生成组件。是否保留旧数据入口与是否允许用户新建旧版脚本可以分别决定。旧版编辑器与新版字段的映射待核对。 | [添加菜单原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-16) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |

## D　图片

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| D01 | 从空卡开始生图 | 选中图片生成卡，在卡片下方编辑提示词、选择模型。实测 Lib Image 2.5 Pro 的规格面板有 13 种画幅，低、标准、高、超高、极致五档画质，1K、2K、4K 三档分辨率，自动、保留、透明三种背景，以及 1、2、4 张。点选这些值决定本次图片的输出规格。 | 实测 · [生成器原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-14) |
| D02 | 使用参考图或增强图片 | 空卡的「图生图」打开选图。「图片高清」使用示例图建立增强节点。普通生图、已有图片和增强任务分别保留自己的输入和操作面板。 | [图片原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-9) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| D03 | 控制画面风格和选区 | 风格、配色和构图选择器为提示词增加指导。相机入口要求模型支持 `cameraControl`。支持局部焦点操作时，用户在画布上选取区域，当前图片保留为编辑目标。节奏属于视频指导。 | [图片生成器](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=16-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/2c4qtlc_w29si.formatted.js) |
| D04 | 修改已有图片 | 图片工具栏把高清、扩图、局部重绘、擦除、抠图和裁切分别关联到独立操作。代码按图片原始尺寸决定按钮可用状态。重新打光、机位变化、网格拆图也有相应入口。每种工具的编辑页、确认和取消结果待补图。 | [图像操作代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) · [指南出处](LibTV画布与生成体系调研.md#图像生成与编辑) |

## E　视频生成和成片处理

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| E01 | 用首尾帧决定镜头边界 | 空节点上的首尾帧预设创建两个上游示例图和两条连线，写入预设提示词并设置顺序。选中视频卡后，卡片下方同时显示「首帧」「尾帧」和可编辑镜头描述。首帧、尾帧和剧本文字分别承担画面边界、动作和叙述的作用。 | [预设原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-11) · [带图和提示词的选中态](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| E02 | 决定输入如何参与生成 | 点底栏的模型与模式。Wan 3.0 实测列表依次为文生视频、全能参考、首尾帧、视频续写。Seedance 2.5 Flash 实测为文生视频、全能参考、图生视频、首尾帧、图片参考、视频编辑、视频续写、超长视频 Beta。缺少输入时相关项变灰。 | [Wan 原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-5) · [Seedance 原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-3) |
| E03 | 改现有视频或从现有视频续写 | 以视频为源，填写想修改的内容、时间段或延续方向。能否再接图片、声音或其他视频取决于模型和具体模式。严格首尾帧给图像指定边界，全能参考可给不同素材指定人物、动作或声音用途。原版素材组合的实际接收上限需按模型逐一核验。 | [公开创作者连线样本](LibTV画布与生成体系调研.md#创作者公开画布) · [模型与参考处理代码](../../.intermediate/libtv-public-component-audit-20261123/3crez0-u4yl4i.formatted.js) |
| E04 | 选择风格、配色、构图、节奏 | 选中已有视频卡后打开面板上方的对应入口。选择后节点记录名称或预览，可清除。节奏选择器可预览片段。确认选择、清除与调换节点后的画面应分别审阅。 | [风格弹层原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-2) · [代码](../../.intermediate/libtv-public-component-audit-20261123/03uiohkeja4lu.formatted.js) |
| E05 | 选择相机与镜头 | 点「相机」打开机身、镜头、焦距和光圈四列，每一列都可选 Auto。按确认将当前组合应用到视频节点。 | 实测 · [入口原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/03uiohkeja4lu.formatted.js) |
| E06 | 加入本次生成的素材 | 「上传」可选本地或已有素材，本地文件可创建并连接素材节点。「画布参考」切到目标选材模式。「标记」随模型的 `magic` 能力显示。「预设提示词」打开官方库或个人库，可搜索和添加。四个入口各有独立作用。 | [入口原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [公共选材代码](../../.intermediate/libtv-public-component-audit-20261123/03uiohkeja4lu.formatted.js) · [视频代码](../../.intermediate/libtv-public-component-audit-20261123/3crez0-u4yl4i.formatted.js) |
| E07 | 修改镜头描述并点名素材 | 在缩略图下方直接编辑提示词，用 `@` 引用已接入的素材。字数统计包含输入内容与已连入的文字。翻译、分镜提示、运镜、主体和效果按模型及模式显示。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/3crez0-u4yl4i.formatted.js) |
| E08 | 设置输出并查看可提交状态 | 底栏有画幅、清晰度、时长、数量和声音。示例为 Auto、720P、5 秒、1 个。右端生成按钮在该示例显示 140 积分。高级菜单可按模型出现联网搜索、素材自动校验、AutoLink 或提示词扩写。这些数值及入口属于当前示例。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [代码](../../.intermediate/libtv-public-component-audit-20261123/3crez0-u4yl4i.formatted.js) |
| E09 | 从成片截取画面或片段 | 有视频后可以提取首帧、尾帧、当前帧，或选择入点与出点截取片段。代码中片段截取要求时长不超过 10 分钟、短边不超过 1080 像素，元数据尚在读取时显示原因。抽帧结果可继续作为下游图像参考。 | [成片工具代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |
| E10 | 处理成片画面与声音 | 有视频后，工具栏分别绑定片段重拍、裁切、高清、镜头拆解、音画分离、人声分离、去字幕、画面编辑、抠像与下载等操作。区域去字幕允许指定范围，已有区域可撤销或重选。开关、源片信息和处理状态决定显示或禁用。处理后的真实媒体与再次打开的画面待核验。 | [成片工具及去字幕代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |

## F　音频

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| F01 | 生成配音旁白 | 选中音频生成节点，在左列选择「配音旁白」，填写台词，选音色、表现力、语言、模型和规格。播放器供试听已有音频。 | [配音原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-17) · [音频卡原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-4) |
| F02 | 生成音乐配乐 | 实测 Mureka V8 可选音乐风格，勾选「纯乐器」后演唱音色置灰。面板提供上传、画布参考和预设提示词。前端另有歌词输入分支，切换工具标签时按工具保留草稿。生成结果回填待核验。 | 实测 · [代码](../../.intermediate/libtv-public-component-audit-20261123/23buf4igm98oi.formatted.js) |
| F03 | 生成音效 | 实测 Seed Audio 1.0 的音效页提供脚步、开门、雨、风等描述示例，规格栏显示中文、24k、wav。用户可编辑描述再生成音频。实际试听结果待核验。 | 实测 · [代码](../../.intermediate/libtv-public-component-audit-20261123/23buf4igm98oi.formatted.js) |
| F04 | 设计音色 | 实测可用 20～1000 字描述希望听到的音色，页面注明每次生成一段试听音频。前端另有音色克隆分支，含录音入口，需要相应模型、权限和可编辑状态。设计音色与克隆音色可以分别评审。 | 实测 · [代码](../../.intermediate/libtv-public-component-audit-20261123/23buf4igm98oi.formatted.js) |
| F05 | 对已有音频继续编辑 | 选择已有音频的编辑操作后，建立下游音频编辑节点，记录源音频和来源节点，自动连线并选中下游。歌词处理还有独立任务状态。各入口在具体账号中的可见性、完成后的结果待核验。 | [代码](../../.intermediate/libtv-public-component-audit-20261123/1tjx0-9ctxmqp.formatted.js) |

## G　智能剪辑与视频合成

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| G01 | 从素材开始剪辑 | 智能剪辑卡展示素材混剪入口。源代码还列有讲解视频、批量广告、口播等模式。账号开放的模式和入口顺序仍待核对。 | [空卡原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-7) · [代码](../../.intermediate/libtv-public-component-audit-20261123/07nqyl8nponps.formatted.js) |
| G02 | 拼接与混音 | 指南记录了单独的「视频合成」入口，输入至少两段视频或视频加音频。时间轴按视频源、音频源、片段、字幕组织。用户可调整顺序、裁切片段、移动播放头、处理原声与独立音轨及淡入淡出。 | [指南出处](LibTV画布与生成体系调研.md#音频与剪辑) · [时间轴代码](../../.intermediate/libtv-public-component-audit-20261123/07nqyl8nponps.formatted.js) |
| G03 | 导出剪辑结果 | 导出面板可选尺寸、帧率、容器和分辨率，并提供到文件或画布的输出。实际导出的文件格式、声音和回填位置仍待走完操作核验。 | [导出代码](../../.intermediate/libtv-public-component-audit-20261123/js/1_4-90dnbtn32.js) |

## H　3D 导演台

| 编号 | 用户要完成的事 | 入口、输入与结果 | 证据 |
| --- | --- | --- | --- |
| H01 | 搭建人物和场景 | 在导演台卡内点「打开导演台」进入独立工作区。人物、姿势、道具、分组和环境占据 3D 空间。可切换导演视角和机位视角，通过三轴控件调整对象。 | [原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-8) |
| H02 | 引入现有 3D 素材 | 普通模型导入分支接受 GLB、GLTF、OBJ、FBX、STL、DAE。高斯场景另支持 PLY、SPLAT、SPZ、KSPLAT，并对 PLY 类型做判断。全景背景有独立入口。具体格式导入、再次打开场景的表现待实测。 | [导演台代码](../../.intermediate/libtv-public-component-audit-20261123/js/3nu_ic217gy39.js) |
| H03 | 调整机位和动作 | 可为人物、道具、摄像机和分组创建轨道，在时间轴记录位置、姿势、视野或注视目标的关键帧，调整 3D 场景中的运动路径。电影指导中的相机参数见 E05，该入口设置镜头提示词。 | [导演台代码](../../.intermediate/libtv-public-component-audit-20261123/js/3nu_ic217gy39.js) |
| H04 | 将机位画面送回素材画布 | 单个或多个机位截图可回填为图片参考。动画路径可按 30 或 60 FPS 输出无声 MP4 到画布。两条输出分别供静态分镜和运动参考使用。实际出图、导出视频和重开项目仍待核验。 | [导演台代码](../../.intermediate/libtv-public-component-audit-20261123/js/3nu_ic217gy39.js) |

## P　画布外围入口与导航

「资产库」「节点列表」「创作广场」是归档的 V2 画布里三个独立入口。另有公开分享画面与登录态样本。各版本的入口位置和文字按各自证据标记。此节分析用户操作及其结果，未对 LibTV 提交模型任务。

| 编号 | 用户要完成的事 | 入口、输入与结果 | 已核对与待核对 |
| --- | --- | --- | --- |
| P01 | 在同一项目切换工作流与故事板 | 工作流展示节点、连线和分组。切换故事板后按项目素材浏览，图片可分角色、场景、物品等，视频可分片段与成片。V2 的视图菜单也提供故事板入口，带可用条件。 | [公开画布](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-8) · [故事板原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-7)。切回时能否恢复先前的选中卡片待实测。 |
| P02 | 在节点列表定位现有内容 | V2 左侧「节点列表」打开抽屉。列表取当前画布节点，按名称搜索、按类型筛选，可展开分组、切换网格或列表并定位节点。再次点入口或关闭按钮可收起。 | 代码。筛选无结果与空画布有独立提示。拖动条目对画布层级的具体影响待实测。[导航与节点列表代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P03 | 打开资产库管理与复用媒体 | 点击「资产库」进入侧栏，可展开为完整管理器，再收回侧栏。入口维护生成历史、上传、已保存资产、当前文件夹及筛选。默认设置把历史和上传设为当前项目，实际初始范围还经过资源查询。 | 代码。界面根据项目、用户及团队记忆当前分类；展开偏好单独保存。跨项目或团队资源的实际可见范围待实测。[资产库代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P04 | 从生成历史选用一份旧素材 | 添加节点菜单中「添加资源 → 从生成历史选择」进入选择模式。发起位置传入可接受的媒体类型。点选符合条件的结果后返回当前操作并关闭历史面板；媒体类型不符时保留面板并提示。 | [入口原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-16) · [选择回调代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js)。打开资产库浏览历史与为当前操作选材可以分别评审，历史条目删除或重新生成待实测。 |
| P05 | 在创作广场打开角色造型室 | V2 引导把「角色造型室」指向创作广场；「全部功能」也注册了角色入口。可描述角色并选参考图，或导入已有角色，填写补充说明并确认形象使用授权。已就绪的角色支持编辑脸部、身材、服装与添加到画布。 | 代码。可从本地或我的资产取参考，有生成中、就绪、失败和离开未保存编辑的状态。实际角色成品、可复用的音色与回填形态待实测。[造型室代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P06 | 从全部功能查找并启动工具 | 点击「全部功能」可展开或收起功能目录。媒体操作绑定图片、视频或音频。选择器可要求先补一种素材；确定后优先定位画布上对应节点，或把所选资产送入画布，再执行操作。公共工具还能打开工作流工具箱、角色造型室、剧本预设等。 | 代码。只读画布与素材种类会限制执行。可见工具的完整目录及账号差异待实测。[全部功能代码](https://liblibai-web-static.liblib.cloud/liblibtv_online/static/_next/static/chunks/28jhtxt9co4fl.js) |
| P07 | 与 TV Director 对话并接入画布素材 | 空画布提示输入创作诉求，已有节点时可展开「创作 Agent」。用户可输入自然语言，或把所选节点发送到对话；编辑器传递所选节点标识。对话历史支持搜索、选择与重命名；工作流支持停靠或浮窗，故事板可切换对话位于左侧或右侧。 | 代码。这里的输入是创作要求与选中内容。请求后如何分工、何时确认、关闭后恢复到哪条消息，以及生成结果是否回填，待在真实会话核实。[对话与节点操作代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P08 | 从 3D 导演台安排机位 | 通过「新增节点 → 导演台」创建卡片，再点「打开导演台」。人物、道具和相机进入三维场景；可调整姿势、机位与关键帧。机位图和无声视频有送回画布的代码路径。 | [导演台原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=19-8) · [3D 执行代码](../../.intermediate/libtv-public-component-audit-20261123/js/3nu_ic217gy39.js)。导出后的实际文件与再次打开的场景状态待实测。 |
| P09 | 在生成器里使用影视指导 | 单选视频节点，点击风格、配色、构图、节奏或相机，选择本镜的生成设置。相机面板有机身、镜头、焦距和光圈，点确认后应用。此处编辑的是当前镜头。 | [完整面板](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=26-3) · [选择器代码](../../.intermediate/libtv-public-component-audit-20261123/03uiohkeja4lu.formatted.js)。五个入口均有实际打开记录。跨节点切换后的设置保留情况待实测。 |
| P10 | 选择节点或平移画布 | 工具菜单列出「移动 V」「抓手工具 H」。移动用于操作节点，抓手用于拖动画布视野。快捷键注册限定在已解锁的画布。快捷键面板同时提供鼠标、触控板下的移动说明。 | 代码。每种工具的选中和光标状态可分别评审；工具切换前后已有选择和输入框焦点的保留情况待实测。[工具与事件代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P11 | 查看键盘快捷键 | 点击快捷键入口关闭其他工具面板并显示快捷键表。表内按创作、缩放、移动、其他分组，按当前可显示的指令和平台渲染。再次点击或使用关闭按钮可返回画布。 | 代码。缩放预设和精确百分比有[原图](https://www.figma.com/design/1yAfX7nImYlNHvbRyegJ78?node-id=13-2)。快捷键在编辑文字时是否触发，需针对各组合实测。[快捷键面板代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |
| P12 | 进入教程与帮助 | 在快捷工具中点击「教程」打开帮助菜单；「使用教程」「LibTV 学院」打开新标签页。同一菜单另有联系客服、联系销售和关注公众号。点击菜单外部收起，工作区仍在。 | 代码。三个联系入口各自有动作；当前账号实际能打开的教程、客服窗口和联系方式待核验。[帮助菜单代码](../../.intermediate/libtv-public-component-audit-20261123/24cywatvj0-3x.formatted.js) |

## 审阅时可以验证的连续操作

1. **一份剧本，两段镜头。** 从 C01 建立脚本，经 C02、C04 分出两个镜头。逐段检查视频卡显示的本镜文字、参考图和动作描述。SceneTurn 对这两段视频另有连续性要求，需要三张边界图，两段共用中间帧。LibTV 的 E01 原图展示单个镜头的首尾帧。
2. **一段源视频，附一张替换图。** 从 A03 给同一张视频卡连接源视频、参考图和文字。用 E02、E03 选支持该组合的模型及编辑模式，再检查 E06、E07 对三个输入的用途标识。参考素材的存在、用途与模型可接受的数量应分别核对。
3. **修改单个镜头的风格。** 从 E04 或 E05 选定设置，关闭弹层，切换到其他卡片再回来。核对选中项目、原提示词及已有连线。用 A07 对照创建副本后的连线继承情况。逐个视频工具的取消、重选状态仍需在原版取证。
4. **拿已有结果继续创作。** 从 E09 取出视频尾帧，再用 A04 接入下一镜，核对图像节点、来源关系及下游首帧。另一条操作从 G02 时间轴预览到 G03 导出，验看输出是否出现在指定位置。

每个编号都可单独标记「保留」「删减」或写明修改内容，例如「E05 保留，F04 仅保留文字设计音色」。涉及空白、选中、展开、取消、确认和重新打开的画面可分别批注。评审结论以产品负责人的批注为准。
