import { describe, expect, it } from 'vitest';
import { buildShotFrameGraph } from '../../app/_features/creation-canvas/plot-shots';
import { CanvasNodeType } from '../../app/_features/creation-canvas/upstream/canvas';

describe('手动分镜展开', () => {
  it('三段剧本建立四张边界图，并把每段文字送入对应视频', () => {
    const anchor = { id: 'story', type: CanvasNodeType.Text, title: '车站故事', position: { x: 0, y: 0 }, width: 340, height: 240 };
    const shots = [
      { no: 1, text: '主角从石阶走到站台。' },
      { no: 2, text: '车灯亮起，主角转身。' },
      { no: 3, text: '列车靠站，车门打开。' },
    ];
    const graph = buildShotFrameGraph(anchor, shots, []);
    expect(graph.videoCount).toBe(3);
    expect(graph.nodes.filter((node) => node.type === CanvasNodeType.Image)).toHaveLength(4);

    let previousTail: string | undefined;
    for (const shot of shots) {
      const config = graph.nodes.find((node) => node.id === `plot-story-${shot.no}-video-config`);
      expect(config?.metadata?.videoSourceMode).toBe('frames');
      const first = config?.metadata?.firstFrameNodeId;
      const last = config?.metadata?.lastFrameNodeId;
      expect(first).toBeTruthy();
      expect(last).not.toBe(first);
      if (previousTail) expect(first).toBe(previousTail);
      const sources = graph.connections.filter((edge) => edge.toNodeId === config?.id).map((edge) => edge.fromNodeId);
      const textId = `plot-story-${shot.no}-text`;
      expect(sources).toEqual([textId, first, last]);
      expect(config?.metadata?.composerContent).toContain(`@[node:${textId}]`);
      previousTail = last;
    }
  });
});
