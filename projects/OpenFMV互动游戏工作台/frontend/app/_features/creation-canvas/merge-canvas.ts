import type { CanvasSnapshot } from '@/app/_utils/cloudApi';

export const sameValue = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
const objectValue = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);

function mergeValue(base: unknown, local: unknown, remote: unknown): unknown {
  if (sameValue(local, base)) return remote;
  if (sameValue(remote, base) || sameValue(local, remote)) return local;
  if (!objectValue(local) || !objectValue(remote)) return local;
  const previous = objectValue(base) ? base : {};
  return Object.fromEntries([...new Set([...Object.keys(local), ...Object.keys(remote)])].flatMap((key) => {
    const value = mergeValue(previous[key], local[key], remote[key]);
    return value === undefined ? [] : [[key, value]];
  }));
}

function mergeItems<T extends { id: string }>(base: T[], local: T[], remote: T[]): T[] {
  if (sameValue(local, base)) return remote;
  const previous = new Map(base.map((item) => [item.id, item]));
  const incoming = new Map(remote.map((item) => [item.id, item]));
  const currentIds = new Set(local.map((item) => item.id));
  return [
    ...local.flatMap((item) => {
      const next = incoming.get(item.id), before = previous.get(item.id);
      if (!next && before && sameValue(item, before)) return [];
      return [mergeValue(before, item, next) as T];
    }),
    ...remote.filter((item) => !currentIds.has(item.id) && !previous.has(item.id)),
  ];
}

export function mergeCanvasSnapshots(base: CanvasSnapshot, local: CanvasSnapshot, remote: CanvasSnapshot): CanvasSnapshot {
  if (base.sceneId !== local.sceneId || local.sceneId !== remote.sceneId) throw new Error('无法合并不同场景的画布。');
  const nodes = mergeItems(base.nodes, local.nodes, remote.nodes);
  const nodeIds = new Set(nodes.map((node) => node.id));
  return {
    sceneId: remote.sceneId,
    nodes,
    connections: mergeItems(base.connections, local.connections, remote.connections).filter((edge) => nodeIds.has(edge.fromNodeId) && nodeIds.has(edge.toNodeId)),
    viewport: mergeValue(base.viewport, local.viewport, remote.viewport) as CanvasSnapshot['viewport'],
  };
}
