import { create } from 'zustand';
import {
  applyEdgeChanges,
  applyNodeChanges,
  Connection,
  OnConnect,
  OnEdgesChange,
  OnNodesChange,
} from '@xyflow/react';

import { AppEdge, AppNode, NodeTimeline, SceneTurnGraph, SceneTurnProject } from '@/app/_types';
import { filterEdgesForNodes } from '@/app/_utils/graphRules';
import { getLocalProject, saveLocalProject } from '@/app/_utils/localProjects';
import { createProjectSnapshot, defaultGraphData, ensureGraphData } from '@/app/_utils/projectPersistence';
import { NODE_DEFAULT_OUTPUT_ID, syncTimelineOutputEdges, upsertTimelineOutputEdge } from '@/app/_utils/timelineOutputEdges';
import { PROJECT_ASSETS_SAVED_EVENT, getCloudProject, saveCloudProject } from '@/app/_utils/cloudApi';

type MaybePromise<T> = T | Promise<T>;

export interface ProjectSessionSaveOptions {
  projectDirectory?: string;
}

export interface ProjectSessionRepository {
  getProject: (id: string) => MaybePromise<SceneTurnProject | null>;
  saveProject: (project: SceneTurnProject) => MaybePromise<SceneTurnProject>;
}

export type ProjectSessionStatus = 'idle' | 'loading' | 'ready' | 'saving' | 'error';

export interface ProjectSessionState {
  project: SceneTurnProject | null;
  projectId: string | null;
  title: string;
  nodes: AppNode[];
  edges: AppEdge[];
  assets: SceneTurnProject['assets'];
  status: ProjectSessionStatus;
  error: string | null;
  revision: number;
  savedRevision: number;
  dirty: boolean;
  loadProject: (projectId: string | null | undefined) => Promise<void>;
  loadProjectSnapshot: (project: SceneTurnProject | null) => void;
  setTitle: (title: string) => void;
  setGraph: (graph: SceneTurnGraph) => void;
  setNodes: (nodes: AppNode[]) => void;
  setEdges: (edges: AppEdge[]) => void;
  onNodesChange: OnNodesChange<AppNode>;
  onEdgesChange: OnEdgesChange<AppEdge>;
  onConnect: OnConnect;
  addNode: (node: AppNode) => void;
  addNodeAndConnect: (node: AppNode, connection: Connection) => void;
  updateNodeData: (id: string, data: Partial<AppNode['data']>) => void;
  updateNodeTimeline: (id: string, timeline: NodeTimeline) => void;
  removeNode: (id: string) => void;
  saveNow: (options?: ProjectSessionSaveOptions) => Promise<SceneTurnProject | null>;
  flushPendingChanges: () => Promise<SceneTurnProject | null>;
  getGraphSnapshot: () => SceneTurnGraph;
  resetSession: () => void;
}

const DEFAULT_TITLE = 'Untitled project';

const cloneGraph = (graph: SceneTurnGraph): SceneTurnGraph => (
  JSON.parse(JSON.stringify(graph)) as SceneTurnGraph
);

const normalizeGraph = (graph?: Partial<SceneTurnGraph> | null): SceneTurnGraph => {
  const normalized = ensureGraphData(graph);
  return {
    nodes: normalized.nodes,
    edges: syncTimelineOutputEdges(normalized.nodes, filterEdgesForNodes(normalized.edges.map((edge) => ({ ...edge, sourceHandle: edge.sourceHandle ?? NODE_DEFAULT_OUTPUT_ID })), normalized.nodes)),
  };
};

const createInitialSessionGraph = () => normalizeGraph(defaultGraphData());

const createDirtyPatch = (revision: number) => ({
  revision: revision + 1,
  dirty: true,
});

const nodeDisplayFields = new Set(['selected', 'dragging', 'resizing', 'measured']);
const edgeDisplayFields = new Set(['selected']);
const contentFields = (value: object, displayFields: ReadonlySet<string>) => (
  Object.fromEntries(Object.entries(value).filter(([key]) => !displayFields.has(key)))
);
const graphContent = (graph: SceneTurnGraph) => JSON.stringify({
  nodes: graph.nodes.map((node) => contentFields(node, nodeDisplayFields)),
  edges: graph.edges.map((edge) => contentFields(edge, edgeDisplayFields)),
});

const retainDisplayState = <T extends { id: string }>(saved: T[], current: T[], displayFields: ReadonlySet<string>) => {
  const currentById = new Map(current.map((item) => [item.id, item]));
  return saved.map((item) => {
    const displayed = currentById.get(item.id);
    return displayed ? {
      ...contentFields(item, displayFields),
      ...Object.fromEntries(Object.entries(displayed).filter(([key]) => displayFields.has(key))),
    } as T : item;
  });
};

const createCleanProjectPatch = (project: SceneTurnProject | null) => {
  const graph = normalizeGraph(project?.graphData ?? defaultGraphData());
  return {
    project,
    projectId: project?.id ?? null,
    title: project?.title ?? DEFAULT_TITLE,
    nodes: graph.nodes,
    edges: graph.edges,
    assets: project?.assets ?? [],
    status: 'ready' as const,
    error: null,
    revision: 0,
    savedRevision: 0,
    dirty: false,
  };
};

export const localProjectSessionRepository: ProjectSessionRepository = {
  getProject: (id) => getLocalProject(id),
  saveProject: (project) => saveLocalProject(project),
};

export const cloudProjectSessionRepository: ProjectSessionRepository = {
  getProject: async (id) => (await getCloudProject(id)).project,
  saveProject: async (project) => (await saveCloudProject(project)).project,
};

export const createProjectSessionStateCreator = (repository: ProjectSessionRepository) => (
  set: (partial: ProjectSessionState | Partial<ProjectSessionState> | ((state: ProjectSessionState) => ProjectSessionState | Partial<ProjectSessionState>)) => void,
  get: () => ProjectSessionState
): ProjectSessionState => {
  const applyGraph = (nodes: AppNode[], edges: AppEdge[]) => {
    const normalizedGraph = normalizeGraph({ nodes, edges });
    set((state) => ({
      nodes: normalizedGraph.nodes,
      edges: normalizedGraph.edges,
      ...(graphContent(state) === graphContent(normalizedGraph) ? {} : createDirtyPatch(state.revision)),
    }));
  };

  return {
    project: null,
    projectId: null,
    title: DEFAULT_TITLE,
    ...createInitialSessionGraph(),
    assets: [],
    status: 'idle',
    error: null,
    revision: 0,
    savedRevision: 0,
    dirty: false,

    loadProject: async (projectId) => {
      if (!projectId) {
        const current = get();
        if (current.status !== 'idle' && current.status !== 'error') return;
        set(createCleanProjectPatch(null));
        return;
      }

      const current = get();
      if (current.projectId === projectId && current.status !== 'idle' && current.status !== 'error') {
        return;
      }

      set({ status: 'loading', error: null });
      try {
        const project = await repository.getProject(projectId);
        set(createCleanProjectPatch(project));
        if (!project) set({ projectId, status: 'ready' });
      } catch (error) {
        set({
          status: 'error',
          error: error instanceof Error ? error.message : 'Failed to load project',
        });
      }
    },

    loadProjectSnapshot: (project) => {
      set(createCleanProjectPatch(project));
    },

    setTitle: (title) => {
      set((state) => (
        state.title === title
          ? {}
          : {
              title,
              ...createDirtyPatch(state.revision),
            }
      ));
    },

    setGraph: (graph) => {
      applyGraph(graph.nodes, graph.edges);
    },

    setNodes: (nodes) => {
      const current = get();
      const nextNodes = Array.isArray(nodes) && nodes.length > 0 ? nodes : defaultGraphData().nodes;
      applyGraph(nextNodes, filterEdgesForNodes(current.edges, nextNodes));
    },

    setEdges: (edges) => {
      const current = get();
      const filteredEdges = filterEdgesForNodes(edges, current.nodes);
      applyGraph(current.nodes, filteredEdges);
    },

    onNodesChange: (changes) => {
      const current = get();
      const nodes = applyNodeChanges(changes, current.nodes) as AppNode[];
      const filteredEdges = filterEdgesForNodes(current.edges, nodes);
      applyGraph(nodes, filteredEdges);
    },

    onEdgesChange: (changes) => {
      const current = get();
      const filteredEdges = filterEdgesForNodes(applyEdgeChanges(changes, current.edges) as AppEdge[], current.nodes);
      applyGraph(current.nodes, filteredEdges);
    },

    onConnect: (connection) => {
      const current = get();
      const nextEdges = upsertTimelineOutputEdge(connection, current.edges, current.nodes);
      if (nextEdges === current.edges) return;
      applyGraph(current.nodes, nextEdges);
    },

    addNode: (node) => {
      const current = get();
      applyGraph([...current.nodes, node], current.edges);
    },

    addNodeAndConnect: (node, connection) => {
      const current = get();
      const nodesWithNewNode = [...current.nodes, node];

      const nextEdges = upsertTimelineOutputEdge(connection, current.edges, nodesWithNewNode);
      applyGraph(nodesWithNewNode, nextEdges);
    },

    updateNodeData: (id, data) => {
      const current = get();
      const nextNodes = current.nodes.map((node) => (
        node.id === id
          ? { ...node, data: { ...node.data, ...data } as AppNode['data'] }
          : node
      ));
      applyGraph(nextNodes, current.edges);
    },

    updateNodeTimeline: (id, timeline) => {
      get().updateNodeData(id, { timeline });
    },

    removeNode: (id) => {
      const current = get();
      const nodes = current.nodes.filter((node) => node.id !== id);
      const filteredEdges = current.edges.filter((edge) => edge.source !== id && edge.target !== id);
      applyGraph(nodes, filteredEdges);
    },

    saveNow: async (options) => {
      const current = get();
      set({ status: 'saving', error: null });
      try {
        const baseProject = current.project;
        const savingRevision = current.revision;
        const snapshot = createProjectSnapshot(
          baseProject,
          current.title,
          current.nodes,
          current.edges,
          current.assets
        );
        const projectToSave = options?.projectDirectory
          ? {
              ...snapshot,
              metadata: {
                ...snapshot.metadata,
                projectDirectory: options.projectDirectory,
              },
            }
          : snapshot;
        const savedProject = await repository.saveProject(projectToSave);
        const graph = normalizeGraph(savedProject.graphData);
        set((state) => {
          if (state.projectId !== current.projectId) return {};
          const changedDuringSave = state.revision !== savingRevision;
          return {
            project: savedProject,
            projectId: savedProject.id,
            title: changedDuringSave ? state.title : savedProject.title,
            nodes: changedDuringSave ? state.nodes : retainDisplayState(graph.nodes, state.nodes, nodeDisplayFields),
            edges: changedDuringSave ? state.edges : retainDisplayState(graph.edges, state.edges, edgeDisplayFields),
            assets: savedProject.assets ?? [],
            status: 'ready',
            error: null,
            savedRevision: savingRevision,
            dirty: changedDuringSave,
          };
        });
        return savedProject;
      } catch (error) {
        set({
          status: 'error',
          error: error instanceof Error ? error.message : 'Failed to save project',
        });
        throw error;
      }
    },

    flushPendingChanges: async () => {
      const current = get();
      if (!current.dirty) return current.project;
      return current.saveNow();
    },

    getGraphSnapshot: () => {
      const current = get();
      return cloneGraph({ nodes: current.nodes, edges: current.edges });
    },

    resetSession: () => {
      set({
        ...createCleanProjectPatch(null),
        status: 'idle',
      });
    },
  };
};

export const useProjectSessionStore = create<ProjectSessionState>()(
  createProjectSessionStateCreator(cloudProjectSessionRepository)
);

if (typeof window !== 'undefined') {
  window.addEventListener(PROJECT_ASSETS_SAVED_EVENT, ((event: CustomEvent<{ project: SceneTurnProject; previousRevision?: number }>) => {
    const { project, previousRevision } = event.detail;
    const state = useProjectSessionStore.getState();
    if (state.projectId !== project.id || !state.project) return;
    if (state.project.metadata.serverRevision !== previousRevision || project.metadata.serverRevision !== (previousRevision ?? -1) + 1) return;
    useProjectSessionStore.setState({
      project: { ...state.project, assets: project.assets, metadata: { ...state.project.metadata, serverRevision: project.metadata.serverRevision } },
      assets: project.assets,
    });
  }) as EventListener);
}
