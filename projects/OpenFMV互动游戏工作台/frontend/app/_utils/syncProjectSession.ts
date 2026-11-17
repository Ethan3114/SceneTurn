import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { getCloudProject } from './cloudApi';
import { mergeProjectChanges } from './mergeProjectChanges';

export async function syncProjectSession(projectId: string) {
  const remote = await getCloudProject(projectId);
  const state = useProjectSessionStore.getState();
  if (state.projectId !== projectId) return [];
  if (!state.dirty || !state.project) { state.loadProjectSnapshot(remote.project); return []; }
  const local = { ...state.project, title: state.title, graphData: state.getGraphSnapshot(), assets: state.assets };
  const result = mergeProjectChanges(state.project, local, remote.project);
  state.loadProjectSnapshot(result.project);
  useProjectSessionStore.setState({ project: remote.project, dirty: true, revision: state.revision + 1, savedRevision: state.savedRevision });
  return result.conflicts;
}
