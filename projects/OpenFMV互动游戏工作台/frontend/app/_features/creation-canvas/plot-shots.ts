import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from './upstream/canvas';

export type PlotShot = { no: number; text: string };

export function buildShotFrameGraph(anchor: CanvasNodeData, shots: PlotShot[], referenceIds: string[]) {
  const nodes: CanvasNodeData[] = [], connections: CanvasConnection[] = [];
  const connect = (fromNodeId: string, toNodeId: string) => connections.push({ id: `flow-${anchor.id}-${connections.length}`, fromNodeId, toNodeId });
  const metadata = { plotSpawnOf: anchor.id, status: 'idle' as const };
  for (const [index, shot] of shots.entries()) {
    const x = anchor.position.x + anchor.width + 80, y = anchor.position.y + index * 560;
    const prefix = `plot-${anchor.id}-${shot.no}`;
    const textId = `${prefix}-text`, configId = `${prefix}-image-config`, imageId = `${prefix}-image`;
    nodes.push(
      { id: textId, type: CanvasNodeType.Text, title: `镜头 ${shot.no}`, position: { x, y }, width: 300, height: 220, metadata: { ...metadata, content: shot.text, status: 'success', fontSize: 14 } },
      { id: configId, type: CanvasNodeType.Config, title: `镜头 ${shot.no} 分镜图`, position: { x: x + 380, y }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'image', dramaTask: 'shotImage', outputNodeId: imageId, composerContent: `根据 @[node:${textId}] 生成这一镜画面，人物、场景和道具遵循已连接参考。`, count: 1 } },
      { id: imageId, type: CanvasNodeType.Image, title: `镜头 ${shot.no} 画面`, position: { x: x + 800, y: y + 80 }, width: 340, height: 240, metadata: { ...metadata, pendingResult: true } },
    );
    connect(anchor.id, textId); connect(textId, configId); connect(configId, imageId);
    referenceIds.forEach((id) => connect(id, configId));
    if (index === 0) continue;
    const previous = shots[index - 1];
    const videoConfigId = `${prefix}-video-config`, videoId = `${prefix}-video`;
    nodes.push(
      { id: videoConfigId, type: CanvasNodeType.Config, title: `镜头 ${previous.no}→${shot.no} 视频`, position: { x: x + 1220, y: y - 280 }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'video', dramaTask: 'clip', outputNodeId: videoId, seconds: '5', vquality: '720P', composerContent: '根据前后两镜画面生成连贯的过渡片段。' } },
      { id: videoId, type: CanvasNodeType.Video, title: `镜头 ${previous.no}→${shot.no} 视频`, position: { x: x + 1640, y: y - 200 }, width: 420, height: 236, metadata: { ...metadata, pendingResult: true } },
    );
    connect(`plot-${anchor.id}-${previous.no}-image`, videoConfigId); connect(imageId, videoConfigId); connect(videoConfigId, videoId);
  }
  const videoIds = nodes.filter((node) => node.type === CanvasNodeType.Video).map((node) => node.id);
  if (videoIds.length >= 2) {
    const configId = `plot-${anchor.id}-join`, outputId = `plot-${anchor.id}-final`, y = anchor.position.y + shots.length * 560;
    nodes.push(
      { id: configId, type: CanvasNodeType.Config, title: '拼接成片', position: { x: anchor.position.x + anchor.width + 1300, y }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'video', dramaTask: 'join', outputNodeId: outputId } },
      { id: outputId, type: CanvasNodeType.Video, title: '成片', position: { x: anchor.position.x + anchor.width + 1720, y: y + 80 }, width: 420, height: 236, metadata: { ...metadata, pendingResult: true } },
    );
    videoIds.forEach((id) => connect(id, configId)); connect(configId, outputId);
  }
  return { nodes, connections, videoCount: videoIds.length, totalSeconds: videoIds.length * 5 };
}
