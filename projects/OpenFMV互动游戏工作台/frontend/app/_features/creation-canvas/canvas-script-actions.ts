import type { CanvasSnapshot } from '@/app/_utils/cloudApi';
import type { CanvasScriptRow } from './CanvasScriptTable';
import type { CanvasNodeData, Position } from './upstream/canvas';

export const scriptExample = '《我在盛唐写天下》\n\n深夜办公室里，沈昭昭伏在发亮的电脑前。急促的键盘声渐渐和鼓声重叠。\n\n她睁开双眼，已经跪在大殿中央。百官列立，皇帝要她当廷赋诗。她望见殿外的长安，慢慢站起。';
export const characterExample = '在即将改造的旧城区，林芽经营着修理废弃机械的小店。暴雨让整条街道停电，机械宠物咔咔用储存的能量点亮旧路灯。请按两张角色图保持人物外形一致，拆分成连续镜头。';
export const reverseExample = '根据图片生成结构化中文提示词，包括主体描述、环境、光影、镜头语言、风格关键词。';

export function formatShot(row: CanvasScriptRow, index: number) {
  return [
    `第 ${index + 1} 镜${row.title ? ` ${row.title}` : ''}${row.duration != null ? `（${row.duration} 秒）` : ''}`,
    row.visual && `画面 ${row.visual}`, row.shotSize && `景别 ${row.shotSize}`, row.lighting && `光影氛围 ${row.lighting}`,
    row.action && `动作 ${row.action}`, row.dialogue && `对白 ${row.dialogue}`, row.sound && `音效 ${row.sound}`,
    row.camera && `运镜 ${row.camera}`, row.finalPrompt && `最终提示词 ${row.finalPrompt}`,
  ].filter(Boolean).join('\n');
}

export function linkNodes(fromNodeId: string, toNodeId: string) {
  return { id: crypto.randomUUID(), fromNodeId, toNodeId };
}

export function placeScriptNodes(target: CanvasNodeData, nodes: CanvasNodeData[], obstacles: CanvasNodeData[]) {
  const gap = 40;
  const height = nodes.reduce((total, node) => total + node.height, gap * Math.max(0, nodes.length - 1));
  let y = target.position.y + (target.height - height) / 2;
  return nodes.map((node) => {
    const x = target.position.x - node.width - 80;
    let obstruction = obstacles.find((item) => x < item.position.x + item.width && x + node.width > item.position.x && y < item.position.y + item.height && y + node.height > item.position.y);
    while (obstruction) {
      y = obstruction.position.y + obstruction.height + gap;
      obstruction = obstacles.find((item) => x < item.position.x + item.width && x + node.width > item.position.x && y < item.position.y + item.height && y + node.height > item.position.y);
    }
    const result = { ...node, position: { x, y } };
    y += node.height + gap;
    return result;
  });
}

export function prepareShotNodes(
  canvas: CanvasSnapshot, script: CanvasNodeData, selectedIds: string[], kind: 'image' | 'video',
  make: (kind: 'image' | 'video' | 'text', position: Position) => CanvasNodeData,
) {
  const rows = script.metadata?.scriptRows || [];
  const nodes = [...canvas.nodes];
  const links = [...canvas.connections];
  let first: string | undefined;
  const nextRows = rows.map((row: CanvasScriptRow, index) => {
    if (!selectedIds.includes(row.id)) return row;
    const saved = kind === 'video' ? row.videoConfigId : row.imageConfigId;
    const existing = nodes.find((node) => node.id === saved);
    const text = formatShot(row, index);
    const source = nodes.find((item) => item.id === row.shotTextId) || make('text', { x: script.position.x + script.width + 96, y: script.position.y + 430 * index });
    const sourceIndex = nodes.findIndex((item) => item.id === source.id);
    const updated = { ...source, title: row.title || `镜头 ${index + 1}`, metadata: { ...source.metadata, content: text } };
    if (sourceIndex >= 0) nodes[sourceIndex] = updated;
    else { nodes.push(updated); links.push(linkNodes(script.id, source.id)); }
    const frameFor = (shot?: CanvasScriptRow) => {
      if (!shot) return undefined;
      const imageConfig = nodes.find((item) => item.id === shot.imageConfigId);
      const frameId = shot.referenceNodeId || imageConfig?.metadata?.outputNodeId;
      return nodes.find((item) => item.id === frameId && item.type === 'image' && item.metadata?.storageKey);
    };
    const firstFrame = frameFor(row);
    const lastFrame = kind === 'video' ? frameFor(rows[index + 1]) : undefined;
    if (kind === 'video' && !firstFrame) throw new Error(`请先为第 ${index + 1} 镜添加分镜图，或完成该镜的图片生成。`);
    const position = { x: source.position.x + source.width + 96 + (kind === 'video' ? 480 : 0), y: source.position.y };
    const config = existing ?? make(kind, position);
    if (!existing) {
      config.title = `${row.title || `镜头 ${index + 1}`} · ${kind === 'image' ? '分镜图' : '视频'}`;
      config.metadata = { ...config.metadata, composerContent: `@[node:${source.id}]`, ...(kind === 'video' ? { videoSourceMode: 'frames', firstFrameNodeId: firstFrame?.id, lastFrameNodeId: lastFrame?.id, seconds: String(row.duration || 5) } : {}) };
      nodes.push(config);
      links.push(linkNodes(source.id, config.id));
    }
    if (existing && kind === 'video') {
      const configIndex = nodes.findIndex((item) => item.id === existing.id);
      nodes[configIndex] = { ...existing, metadata: { ...existing.metadata, firstFrameNodeId: firstFrame?.id, ...(lastFrame ? { lastFrameNodeId: lastFrame.id } : {}), seconds: String(row.duration || 5) } };
    }
    if (firstFrame && !links.some((link) => link.fromNodeId === firstFrame.id && link.toNodeId === config.id)) links.push(linkNodes(firstFrame.id, config.id));
    if (lastFrame && !links.some((link) => link.fromNodeId === lastFrame.id && link.toNodeId === config.id)) links.push(linkNodes(lastFrame.id, config.id));
    first ??= config.id;
    return { ...row, shotTextId: source.id, ...(kind === 'image' ? { imageConfigId: config.id } : { videoConfigId: config.id }) };
  });
  return { canvas: { ...canvas, connections: links, nodes: nodes.map((node) => node.id === script.id ? { ...node, metadata: { ...node.metadata, scriptRows: nextRows } } : node) }, first };
}
