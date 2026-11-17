import type { CanvasNodeData } from './upstream/canvas';

export type DramaTaskKind = 'text' | 'image' | 'video';
export type DramaTaskId = 'text' | 'summary' | 'character' | 'prop' | 'scene' | 'plot' | 'shot' | 'characterImage' | 'propImage' | 'sceneImage' | 'shotImage' | 'clip' | 'join';
export type DramaTask = { id: DramaTaskId; kind: DramaTaskKind; label: string; hint: string };

export const DRAMA_TASKS: DramaTask[] = [
  { id: 'text', kind: 'text', label: '剧本创作', hint: '根据创意编写完整剧本，包含场景、人物动作、对白与互动选择。' },
  { id: 'summary', kind: 'text', label: '剧本摘要', hint: '概括这一集的故事、冲突与结局。' },
  { id: 'character', kind: 'text', label: '角色画像', hint: '描述角色的外貌、服装与特征，供角色图使用。' },
  { id: 'prop', kind: 'text', label: '道具提示词', hint: '描述道具的外形、材质与细节。' },
  { id: 'scene', kind: 'text', label: '场景说明', hint: '描述地点、空间、光线与环境。' },
  { id: 'plot', kind: 'text', label: '分镜情节', hint: '拆解每一镜发生的事件与人物行动。' },
  { id: 'shot', kind: 'text', label: '单镜提示词', hint: '结合角色、场景与道具，描述这一镜的画面。' },
  { id: 'characterImage', kind: 'image', label: '角色图', hint: '根据角色描述生成正面、侧面与背面参考图。' },
  { id: 'propImage', kind: 'image', label: '道具图', hint: '根据道具描述生成独立的道具图。' },
  { id: 'sceneImage', kind: 'image', label: '场景图', hint: '根据场景描述生成环境参考图。' },
  { id: 'shotImage', kind: 'image', label: '分镜图', hint: '根据分镜描述与参考素材生成这一镜的画面。' },
  { id: 'clip', kind: 'video', label: '视频片段', hint: '根据文字要求生成一个视频片段，提交前核对费用。' },
  { id: 'join', kind: 'video', label: '成片拼接', hint: '按连接顺序拼接至少两个视频，本机处理，无模型费用。' },
];

export function findDramaTask(id?: string): DramaTask {
  return DRAMA_TASKS.find((task) => task.id === id) || DRAMA_TASKS.find((task) => task.id === 'clip')!;
}

export function nodeDramaTask(node: CanvasNodeData): DramaTask {
  return findDramaTask(node.metadata?.dramaTask);
}
