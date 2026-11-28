import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRuntime } from '../frontend/shared/runtimeCore.mjs';

const envelope = JSON.parse(await readFile(process.argv[2], 'utf8'));
const graph = envelope.project.graphData;
let checked = 0;
for (const scene of graph.nodes.filter((node) => node.type === 'scene')) {
  for (const clip of scene.data.timeline.tracks.flatMap((track) => track.clips).filter((item) => item.type === 'button')) {
    for (const outcome of clip.mode === 'qte' ? ['click', 'timeout'] : ['click']) {
      const runtime = createRuntime(graph, { entryNodeId: scene.id });
      runtime.dispatch({ type: 'timeline.time.update', time: clip.startTime + (outcome === 'timeout' ? clip.duration : 0.01) });
      runtime.dispatch({ type: outcome === 'timeout' ? 'timeline.clip.timeout' : 'timeline.clip.triggered', clipId: clip.id });
      const edge = graph.edges.find((item) => item.source === scene.id && item.sourceHandle === `button:${clip.id}:${outcome}`);
      assert.ok(edge);
      assert.equal(runtime.getSnapshot().currentNodeId, edge.target);
      checked++;
    }
  }
}
assert.ok(checked >= 3);
console.log(JSON.stringify({ phase: 'runtime_verified', realSavedTransitions: checked }));
