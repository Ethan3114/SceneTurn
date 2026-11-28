import { describe, expect, it } from 'vitest';

import { upstreamBranchEdges } from '../../app/_features/creation-canvas/agent-branches';

const edges = [
  { id: 'start', source: 'Start', target: 'Shared' },
  { id: 'choose-a', source: 'Shared', target: 'A' },
  { id: 'choose-b', source: 'Shared', target: 'B' },
  { id: 'a-merge', source: 'A', target: 'Merge' },
  { id: 'b-merge', source: 'B', target: 'Merge' },
  { id: 'other', source: 'Elsewhere', target: 'OtherEnd' },
];

describe('创作助手的剧情路径选择', () => {
  it('合流场景可以选择实际上游分叉，保留稳定连线标识', () => {
    expect(upstreamBranchEdges(edges, 'Merge').map((edge) => edge.id)).toEqual(['choose-a', 'choose-b']);
    expect(upstreamBranchEdges(edges, 'A').map((edge) => edge.id)).toEqual(['choose-a']);
  });
  it('循环和两条目标相同的条件分支保留真实选择', () => {
    const routes = [...edges, { id: 'choose-a-alt', source: 'Shared', target: 'A' }, { id: 'loop', source: 'Merge', target: 'Shared' }];
    expect(upstreamBranchEdges(routes, 'Merge').map((edge) => edge.id)).toEqual(['choose-a', 'choose-b', 'choose-a-alt']);
  });
});
