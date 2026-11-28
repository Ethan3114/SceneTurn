import { CanvasShortcutTable } from '@/app/_features/creation-canvas/CanvasWorkspaceTools';
import '@/app/_features/creation-canvas/libtv-workspace.css';

export default function CanvasGuide() {
  return <main className="fmv-canvas-guide"><h1>素材画布使用教程</h1>
    <h2>准备镜头</h2><p>双击画布空白处，选择「脚本」。可以手动编写镜头表，也可以填写创作要求。每个镜头分别保留画面、动作和对白。</p>
    <h2>连接素材</h2><p>把素材卡片右侧的圆点拖到另一张卡片左侧。拖到空白处可以新建下游节点。选中节点后，在下方的生成面板编辑提示词、模型和规格。</p>
    <h2>制作连续视频</h2><p>先准备相邻镜头共用的边界图，再为每段视频指定首帧和尾帧。选择模型后可以检查它支持的输入。在确认窗口核对本镜头的画面、时长和剧本，再提交生成。</p>
    <h2>整理与复用</h2><p>打开「节点列表」按名称或类型定位卡片。按住 Shift 选择多张卡片，再将它们组合。用「创建资产」将结果保存到项目资产库，或在「版本记录」查看之前保存的内容。</p>
    <h2>使用创作对话</h2><p>选中素材后点击「发送到创作对话」，引用会出现在输入框上方。输入修改要求并发送，可以沿用这次对话继续制作。</p>
    <h2>键盘与鼠标</h2><CanvasShortcutTable/></main>;
}
