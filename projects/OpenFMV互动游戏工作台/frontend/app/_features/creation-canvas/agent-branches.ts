import dagre from 'dagre';

export function upstreamBranchEdges(edges: { id: string; source: string; target: string }[], sceneId: string) {
  const graph = new dagre.graphlib.Graph({ directed: true, multigraph: true });
  graph.setNode(sceneId, {});
  const outgoing = new Map<string, number>();
  for (const edge of edges) {
    graph.setNode(edge.source, {}); graph.setNode(edge.target, {});
    graph.setEdge(edge.target, edge.source, undefined, edge.id);
    outgoing.set(edge.source, (outgoing.get(edge.source) || 0) + 1);
  }
  const ancestors = dagre.graphlib.alg.dijkstra(graph, sceneId);
  return edges.filter((edge) => (outgoing.get(edge.source) || 0) > 1 && Number.isFinite(ancestors[edge.target]?.distance));
}
