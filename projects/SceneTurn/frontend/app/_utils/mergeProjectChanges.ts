import type { SceneTurnProject } from '@/app/_types';

const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const keyed = (values: unknown[]): values is Record<string, unknown>[] => values.every((item) => record(item) && typeof item.id === 'string');

export function mergeProjectChanges(base: SceneTurnProject, local: SceneTurnProject, remote: SceneTurnProject) {
  const conflicts: string[] = [];
  function merge(previous: unknown, edited: unknown, saved: unknown, path: string): unknown {
    if (same(previous, edited)) return saved;
    if (same(previous, saved) || same(edited, saved)) return edited;
    if (Array.isArray(previous) && Array.isArray(edited) && Array.isArray(saved) && keyed(previous) && keyed(edited) && keyed(saved)) {
      const before = new Map(previous.map((item) => [item.id, item]));
      const current = new Map(edited.map((item) => [item.id, item]));
      const latest = new Map(saved.map((item) => [item.id, item]));
      return [...new Set([...current.keys(), ...latest.keys()])].map((id) => merge(before.get(id), current.get(id), latest.get(id), `${path}/${id}`)).filter((item) => item !== undefined);
    }
    if (record(previous) && record(edited) && record(saved)) {
      return Object.fromEntries([...new Set([...Object.keys(previous), ...Object.keys(edited), ...Object.keys(saved)])]
        .map((key) => [key, merge(previous[key], edited[key], saved[key], `${path}/${key}`)]).filter(([, value]) => value !== undefined));
    }
    conflicts.push(path);
    return edited;
  }
  return { project: merge(base, local, remote, '') as SceneTurnProject, conflicts };
}
