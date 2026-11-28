import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from './upstream/canvas';

export type PlotShot = { no: number; text: string };

export function buildShotFrameGraph(anchor: CanvasNodeData, shots: PlotShot[], referenceIds: string[]) {
  const nodes: CanvasNodeData[] = [], connections: CanvasConnection[] = [];
  const connect = (fromNodeId: string, toNodeId: string) => connections.push({ id: `flow-${anchor.id}-${connections.length}`, fromNodeId, toNodeId });
  const metadata = { plotSpawnOf: anchor.id, status: 'idle' as const };
  const x = anchor.position.x + anchor.width + 80;
  const y = anchor.position.y;
  for (const [index, shot] of shots.entries()) {
    const row = y + index * 560;
    const prefix = `plot-${anchor.id}-${shot.no}`;
    const textId = `${prefix}-text`, configId = `${prefix}-image-config`, imageId = `${prefix}-image`;
    nodes.push(
      { id: textId, type: CanvasNodeType.Text, title: `镜头 ${shot.no}`, position: { x, y: row }, width: 300, height: 220, metadata: { ...metadata, content: shot.text, status: 'success', fontSize: 14 } },
      { id: configId, type: CanvasNodeType.Config, title: `镜头 ${shot.no} 首帧`, position: { x: x + 380, y: row }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'image', dramaTask: 'shotImage', outputNodeId: imageId, composerContent: '生成这段镜头开场瞬间的完整画面。按已连接剧本与图片保持人物、光线和构图连续。', count: 1 } },
      { id: imageId, type: CanvasNodeType.Image, title: `镜头 ${shot.no} 首帧结果`, position: { x: x + 800, y: row + 80 }, width: 340, height: 240, metadata: { ...metadata, pendingResult: true } },
    );
    connect(anchor.id, textId); connect(textId, configId); connect(configId, imageId);
    referenceIds.forEach((id) => connect(id, configId));
    const next = shots[index + 1];
    const tailId = next ? `plot-${anchor.id}-${next.no}-image` : `plot-${anchor.id}-last-image`;
    const videoConfigId = `${prefix}-video-config`, videoId = `${prefix}-video`;
    nodes.push(
      { id: videoConfigId, type: CanvasNodeType.Config, title: `镜头 ${shot.no} 视频`, position: { x: x + 1220, y: row }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'video', dramaTask: 'clip', outputNodeId: videoId, videoSourceMode: 'frames', firstFrameNodeId: imageId, lastFrameNodeId: tailId, seconds: '5', vquality: '720P', composerContent: `根据 @[node:${textId}] 的动作、运镜、对白与声音要求，在已指定的首尾画面之间生成本镜头。` } },
      { id: videoId, type: CanvasNodeType.Video, title: `镜头 ${shot.no} 视频结果`, position: { x: x + 1640, y: row + 80 }, width: 420, height: 236, metadata: { ...metadata, pendingResult: true } },
    );
    connect(textId, videoConfigId); connect(imageId, videoConfigId); connect(tailId, videoConfigId); connect(videoConfigId, videoId);
    if (!next) {
      const lastConfig = `plot-${anchor.id}-last-image-config`;
      nodes.push(
        { id: lastConfig, type: CanvasNodeType.Config, title: '末镜尾帧', position: { x: x + 380, y: row + 560 }, width: 340, height: 480, metadata: { ...metadata, generationMode: 'image', dramaTask: 'shotImage', outputNodeId: tailId, composerContent: '根据最后一段剧本，生成动作完成后的定格画面。按已经连接的参考保留角色、道具、场景和摄影风格。', count: 1 } },
        { id: tailId, type: CanvasNodeType.Image, title: '末镜尾帧结果', position: { x: x + 800, y: row + 640 }, width: 340, height: 240, metadata: { ...metadata, pendingResult: true } },
      );
      connect(textId, lastConfig); connect(lastConfig, tailId);
      referenceIds.forEach((id) => connect(id, lastConfig));
    }
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
