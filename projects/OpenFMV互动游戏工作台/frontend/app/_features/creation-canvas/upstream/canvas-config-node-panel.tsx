import { memo, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { ArrowUp, ChevronDown, Film, ImagePlus, Link2, MousePointer2, Plus, SquarePen, Upload, Volume2, VolumeX } from 'lucide-react';

import { canvasImageSize, imageRatioFor } from '../canvas-image-settings';
import { CanvasFilmControls, PromptPresets } from '../CanvasFilmControls';
import { CanvasImageSpecs } from '../CanvasImageSpecs';
import { canvasVideoModeOptions, canvasVideoModeSupported, canvasVideoRatio, canvasVideoRatios, canvasVideoReferenceImageIds, videoSourceLabels, type CanvasVideoSourceMode } from '../canvas-video-settings';
import { nodeDramaTask } from '../drama-tasks';
import type { CanvasNodeData, CanvasNodeMetadata } from './canvas';
import type { NodeGenerationInput } from './canvas-node-generation';
import { CanvasConfigComposer } from './canvas-config-composer';
import { CanvasNodeReferenceBar } from './canvas-node-reference-bar';
import { CanvasVideoSettings } from './canvas-node-prompt-panel';
import type { ModelProfile } from '@/app/_utils/providerApi';
import './canvas-config-card.css';

type Props = {
  ownerId: string;
  onChooseMediaReference: (nodeId: string, method: 'upload' | 'library') => void;
  node: CanvasNodeData;
  nodes: CanvasNodeData[];
  connectedNodes: CanvasNodeData[];
  inputs: NodeGenerationInput[];
  model: string;
  models: ModelProfile[];
  selectedProfile?: ModelProfile;
  defaultProfile?: ModelProfile;
  textServiceLabel: string;
  textConnected: boolean;
  busy: boolean;
  canGenerate: boolean;
  inputProblem: string;
  joinDuration?: { count: number; seconds: number | null; estimated: boolean };
  onConfigChange: (id: string, patch: Partial<CanvasNodeMetadata>) => void;
  onGenerate: (id: string) => void;
  onDisconnectReference: (fromNodeId: string, toNodeId: string) => void;
  onStartReferenceSelection: (id: string) => void;
};

type ImageInput = { nodeId: string; title: string; image?: { dataUrl: string } };
type Menu = 'model' | 'mode' | 'specifications' | 'references' | 'first' | 'last' | 'video' | 'upload' | 'presets';

function GeneratorMenu({ label, testId, open, disabled, onToggle, trigger, slot = false, width, children }: {
  label: string;
  testId?: string;
  open: boolean;
  disabled?: boolean;
  onToggle: () => void;
  trigger: ReactNode;
  slot?: boolean;
  width?: 'small' | 'wide';
  children: ReactNode;
}) {
  const id = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  return <div className="canvas-config-control" onKeyDown={(event) => {
    if (!open) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onToggle();
      buttonRef.current?.focus();
      return;
    }
    const options = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)'));
    if (!options.length || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const focused = options.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : focused < 0 ? 0
      : (focused + options.length + (event.key === 'ArrowDown' ? 1 : -1)) % options.length;
    event.preventDefault();
    event.stopPropagation();
    options[next].focus();
  }}>
    <button ref={buttonRef} type="button" className={slot ? 'canvas-config-reference-slot' : 'canvas-config-menu-trigger'} aria-label={label} title={label} aria-expanded={open} aria-controls={open ? id : undefined} data-testid={testId} disabled={disabled} onClick={onToggle}>
      {trigger}
      {!slot && <ChevronDown size={12} aria-hidden="true" />}
    </button>
    {open && <div id={id} className="canvas-config-popover" data-width={width} data-slot={slot || undefined}>{children}</div>}
  </div>;
}

function VideoReferenceImages({ images, selectedIds, maxImages, inputProblem, onChange, onConnect }: {
  images: ImageInput[];
  selectedIds: string[];
  maxImages: number;
  inputProblem: string;
  onChange: (ids: string[]) => void;
  onConnect: () => void;
}) {
  const headingId = useId();
  const selectedNumbers = new Map(selectedIds.map((id, index) => [id, index + 1]));
  return <section className="canvas-config-video-references" data-testid="video-reference-images" role="group" aria-labelledby={headingId}>
    <div className="canvas-config-video-reference-heading">
      <span id={headingId}>参考图</span>
      <span className="canvas-config-video-reference-count" data-testid="video-reference-count">已选 {selectedIds.length}/{maxImages} 张</span>
    </div>
    {inputProblem && <p className="canvas-config-note canvas-config-note--warning" data-testid="generation-input-problem" role="status">{inputProblem}</p>}
    {images.length > 0 && <>
      <div className="canvas-config-video-reference-list">
        {images.map(({ nodeId, title, image }) => {
          const number = selectedNumbers.get(nodeId);
          return <label className="canvas-config-video-reference" data-selected={number !== undefined} key={nodeId}>
            {image?.dataUrl && <img src={image.dataUrl} alt="" loading="lazy" draggable={false} />}
            <span className="canvas-config-video-reference-title" title={title}>{title}</span>
            <span className="canvas-config-video-reference-number">{number === undefined ? '未选用' : `图${number}`}</span>
            <input type="checkbox" checked={number !== undefined} aria-label={`参考图 ${title}`} onChange={(event) => {
              onChange(event.target.checked ? [...selectedIds, nodeId] : selectedIds.filter((id) => id !== nodeId));
            }} />
          </label>;
        })}
      </div>
      {images.length > 1 && <p className="canvas-config-note">默认按连线顺序编号，重新勾选会排在末尾。</p>}
    </>}
    <button type="button" className="canvas-config-video-reference-connect" onClick={onConnect}><Link2 size={14} aria-hidden="true" />连接参考图</button>
  </section>;
}

function CanvasConfigNodePanelView({ ownerId, onChooseMediaReference, node, nodes, connectedNodes, inputs, model, models, selectedProfile, defaultProfile, textServiceLabel, textConnected, busy, canGenerate, inputProblem, joinDuration, onConfigChange, onGenerate, onDisconnectReference, onStartReferenceSelection }: Props) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [openMenu, setOpenMenu] = useState<Menu | null>(null);
  const task = nodeDramaTask(node);
  const isVideo = task.kind === 'video' && task.id !== 'join';
  const isImage = task.kind === 'image';
  const capabilities = selectedProfile?.capabilities;
  const durationRange = capabilities?.minDuration !== undefined && capabilities.maxDuration !== undefined
    ? [capabilities.minDuration, capabilities.maxDuration] as [number, number] : undefined;
  const imageRatio = imageRatioFor(node.metadata);
  const imageSize = canvasImageSize(node.metadata, selectedProfile);
  const videoMode = node.metadata?.videoSourceMode ?? 'text';
  const linkedMedia = [...new Map(inputs.flatMap((input) => input.type === 'group' ? input.children : [input]).map((item) => [item.nodeId, item])).values()];
  const linkedImages = linkedMedia.filter((input) => input.type === 'image' && input.image?.storageKey);
  const linkedVideos = linkedMedia.filter((input) => input.type === 'video' && input.video?.storageKey);
  const videoOptions = canvasVideoModeOptions(selectedProfile, linkedImages.length, linkedVideos.length);
  const activeVideoOption = videoOptions.find((option) => option.mode === videoMode && option.id === (node.metadata?.videoMode || videoMode));
  const singleFrame = activeVideoOption?.id === 'image-to-video';
  const usesMixedVideo = videoMode === 'reference' || videoMode === 'edit' || videoMode === 'extend';
  const supportedVideoMode = canvasVideoModeSupported(videoMode, selectedProfile);
  const maxReferenceImages = usesMixedVideo && supportedVideoMode ? capabilities?.maxReferenceImages ?? 0 : 0;
  const linkedImageNodeIds = linkedImages.map((input) => input.nodeId);
  const selectedReferenceImageIds = maxReferenceImages ? canvasVideoReferenceImageIds(node.metadata, linkedImageNodeIds) : [];
  const videoMentionLabels = new Map(selectedReferenceImageIds.map((id, index) => [id, `图${index + 1}`]));
  const { firstFrameNodeId, lastFrameNodeId, referenceVideoNodeId } = node.metadata ?? {};
  const waitingForFirstFrame = videoMode === 'frames' && firstFrameNodeId && connectedNodes.some((source) => source.id === firstFrameNodeId)
    && !linkedImageNodeIds.includes(firstFrameNodeId);
  if (videoMode === 'frames' && capabilities?.firstFrame) {
    if (firstFrameNodeId && linkedImageNodeIds.includes(firstFrameNodeId)) videoMentionLabels.set(firstFrameNodeId, '首帧');
    if (capabilities.lastFrame && !singleFrame && lastFrameNodeId && linkedImageNodeIds.includes(lastFrameNodeId)) {
      videoMentionLabels.set(lastFrameNodeId, videoMentionLabels.has(lastFrameNodeId) ? '首帧与尾帧' : '尾帧');
    }
  }
  const selectedReferenceVideo = usesMixedVideo && supportedVideoMode && linkedVideos.find((input) => input.nodeId === referenceVideoNodeId);
  if (selectedReferenceVideo) videoMentionLabels.set(selectedReferenceVideo.nodeId, '视频1');
  const isVideoMention = (input: Exclude<NodeGenerationInput, { type: 'group' }>) => input.type === 'text' || videoMentionLabels.has(input.nodeId);
  const videoMentionInputs = isVideo
    ? inputs.filter((input) => input.type === 'group' ? input.children.every(isVideoMention) : isVideoMention(input))
    : undefined;

  useEffect(() => {
    if (!openMenu || openMenu === 'presets') return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !panelRef.current?.contains(event.target)) setOpenMenu(null);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [openMenu]);

  const toggleMenu = (menu: Menu) => setOpenMenu((current) => current === menu ? null : menu);
  const startReferenceSelection = () => {
    setOpenMenu(null);
    onStartReferenceSelection(node.id);
  };
  const inputSelect = (key: 'firstFrameNodeId' | 'lastFrameNodeId' | 'referenceVideoNodeId', label: string, options: typeof linkedMedia, initial: string) =>
    <label className="canvas-config-field"><span>{label}</span><select aria-label={label} value={options.some((item) => item.nodeId === node.metadata?.[key]) ? node.metadata?.[key] : ''} onChange={(event) => {
      onConfigChange(node.id, { [key]: event.target.value || undefined });
      setOpenMenu(null);
    }}>
      <option value="">{initial}</option>
      {options.map((item) => <option key={item.nodeId} value={item.nodeId}>{item.title}</option>)}
    </select></label>;

  function selectModel(profile: ModelProfile) {
    const modelProfileId = profile.id === defaultProfile?.id ? undefined : profile.id;
    const nextSize = isImage ? canvasImageSize(node.metadata, profile) : undefined;
    const shouldResetVideoMode = isVideo && !canvasVideoModeSupported(videoMode, profile);
    onConfigChange(node.id, {
      modelProfileId,
      ...(nextSize ? { imageWidth: nextSize.imageWidth, imageHeight: nextSize.imageHeight } : {}),
      ...(shouldResetVideoMode ? { videoSourceMode: 'text', videoMode: undefined } : {}),
    });
    setOpenMenu(null);
  }

  function selectVideoMode(id: string, mode: CanvasVideoSourceMode) {
    const useOnlyImage = id === 'image-reference';
    const useVideoSource = mode === 'extend' || mode === 'edit' || (mode === 'reference' && (!linkedImages.length || Boolean(referenceVideoNodeId)));
    const first = linkedImages.find((item) => item.nodeId === firstFrameNodeId) ?? linkedImages[0];
    const source = linkedVideos.find((item) => item.nodeId === referenceVideoNodeId) ?? linkedVideos[0];
    onConfigChange(node.id, {
      videoSourceMode: mode,
      videoMode: id === mode ? undefined : id,
      ...(mode === 'frames' ? { firstFrameNodeId: first?.nodeId } : {}),
      ...(id === 'image-to-video' ? { lastFrameNodeId: undefined } : {}),
      ...(useOnlyImage ? { referenceVideoNodeId: undefined } : useVideoSource ? { referenceVideoNodeId: source?.nodeId } : {}),
    });
    setOpenMenu(null);
  }

  const createSlot = (name: 'first' | 'last' | 'video', label: string, field: 'firstFrameNodeId' | 'lastFrameNodeId' | 'referenceVideoNodeId', options: typeof linkedMedia, caption: string) => {
    const item = options.find(({ nodeId }) => nodeId === node.metadata?.[field]);
    return <GeneratorMenu slot label={`选择${label}`} testId={`generation-source-${name}`} open={openMenu === name} onToggle={() => toggleMenu(name)} width="small" trigger={<>
      {item?.image?.dataUrl ? <img src={item.image.dataUrl} alt="" draggable={false} /> : name === 'video' ? <Film size={16} aria-hidden="true" /> : <ImagePlus size={16} aria-hidden="true" />}
      <span>{label}</span>
    </>}>
      {inputSelect(field, label, options, caption)}
      <button className="canvas-config-option" type="button" onClick={startReferenceSelection}>从画布选择</button>
    </GeneratorMenu>;
  };

  const positionedReferences = isVideo && (videoMode === 'frames' || usesMixedVideo || inputs.length > 0) || (isImage && linkedMedia.length > 0);
  const videoRatio = canvasVideoRatio(node.metadata, selectedProfile);
  const videoSpec = `${videoRatio === 'adaptive' ? '自适应' : videoRatio} ${node.metadata?.vquality || '720P'} ${node.metadata?.seconds || 5}s 1个`;
  const imageSpec = `${imageRatio.id} 标准画质 ${imageSize ? `${imageSize.longEdge / 1024}K ` : ''}1张`;
  const prompt = node.metadata?.composerContent ?? node.metadata?.prompt ?? '';
  const visiblePrompt = prompt.replace(/@\[node:([^\]]+)\]/g, (_, id: string) => '@' + (videoMentionLabels.get(id) ?? connectedNodes.find((item) => item.id === id)?.title ?? '素材'));
  const textCount = Array.from(visiblePrompt).length + linkedMedia.reduce((total, item) => total + (item.type === 'text' ? Array.from(item.text ?? '').length : 0), 0);
  const actionId = task.kind === 'text' ? 'text-generation-start' : task.id === 'join' ? 'generation-join' : task.kind === 'image' ? 'image-generation-prepare' : 'generation-quote';
  const actionLabel = task.kind === 'text' ? `生成${task.label}` : task.id === 'join' ? '拼接成片' : task.kind === 'image' ? '核对生图内容' : '核对生成内容';
  return (
    <div ref={panelRef} className="canvas-config-card" data-testid="generation-config" data-kind={task.id === 'join' ? 'join' : task.kind} data-with-references={positionedReferences || undefined} data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()} onKeyDown={(event) => {
    if (event.key === 'Escape' && openMenu) {
        event.preventDefault();
        event.stopPropagation();
        setOpenMenu(null);
      }
    }}>
      {(isImage || isVideo) && <CanvasFilmControls key={node.id} video={isVideo} ownerId={ownerId} value={node.metadata?.filmGuidance ?? {}} change={(filmGuidance) => onConfigChange(node.id, { filmGuidance })} />}
      <div className="canvas-config-layout">
        {(isImage || isVideo) && <div className="fmv-media-tools">
          <GeneratorMenu label="上传" open={openMenu === 'upload'} onToggle={() => toggleMenu('upload')} trigger={<><Plus size={14} />上传</>}>
            {(['upload', 'library'] as const).map((method) => <button key={method} type="button" className="canvas-config-option" onClick={() => { setOpenMenu(null); onChooseMediaReference(node.id, method); }}><Upload size={13} />{method === 'upload' ? '本地上传' : '素材库'}</button>)}
          </GeneratorMenu>
          <button type="button" onClick={startReferenceSelection}><MousePointer2 size={14} />画布参考</button>
          <button type="button" onClick={() => toggleMenu('presets')}><SquarePen size={14} />预设提示词</button>
        </div>}
        {openMenu === 'presets' && <PromptPresets ownerId={ownerId} close={() => setOpenMenu(null)} apply={(text) => onConfigChange(node.id, { composerContent: [prompt, text].filter(Boolean).join('\n') })} />}
        <div className="canvas-config-main">
          {task.id !== 'join' && <div className="canvas-config-reference-strip" aria-label="素材引用">
            {isVideo && videoMode === 'frames' && <>
              {createSlot('first', '首帧', 'firstFrameNodeId', linkedImages, linkedImages.length ? '选择已连接的图片' : '先连接图片')}
              {capabilities?.lastFrame && !singleFrame && createSlot('last', '尾帧', 'lastFrameNodeId', linkedImages, '不固定尾帧')}
            </>}
            {isVideo && usesMixedVideo && activeVideoOption?.id !== 'image-reference' && createSlot('video', videoMode === 'reference' ? '参考视频' : '原视频', 'referenceVideoNodeId', linkedVideos, videoMode === 'reference' && maxReferenceImages > 0 ? '选用已连接的视频' : '先连接视频')}
            {(isImage || maxReferenceImages > 0) && linkedImages.filter(({ nodeId }) => isImage || selectedReferenceImageIds.includes(nodeId)).map(({ nodeId, title, image }) => (
              <span className="canvas-config-reference-thumbnail" title={title} key={nodeId}>{image?.dataUrl ? <img src={image.dataUrl} alt={title} loading="lazy" draggable={false} /> : <ImagePlus size={16} aria-hidden="true" />}</span>
            ))}
            {(!(isImage || isVideo) || linkedMedia.length > 0 || usesMixedVideo || videoMode === 'frames') && <GeneratorMenu slot label="添加参考素材" testId="generation-references-trigger" open={openMenu === 'references'} onToggle={() => toggleMenu('references')} width="wide" trigger={<Plus size={16} aria-hidden="true" />}>
              <CanvasNodeReferenceBar nodeId={node.id} nodes={nodes} connectedNodes={connectedNodes} onDisconnect={onDisconnectReference} onStartSelection={startReferenceSelection} />
              {maxReferenceImages > 0 && <VideoReferenceImages images={linkedImages} selectedIds={selectedReferenceImageIds} maxImages={maxReferenceImages} inputProblem={inputProblem} onChange={(videoReferenceImageNodeIds) => onConfigChange(node.id, { videoReferenceImageNodeIds })} onConnect={startReferenceSelection} />}
            </GeneratorMenu>}
          </div>}
          {task.id !== 'join' && <CanvasConfigComposer embedded nodeId={node.id} nodes={nodes} value={node.metadata?.composerContent ?? node.metadata?.prompt ?? ''} inputs={inputs} mentionInputs={videoMentionInputs} mentionLabels={task.kind === 'video' ? videoMentionLabels : undefined} connectedNodes={connectedNodes} onChange={(composerContent) => onConfigChange(node.id, { composerContent })} onDisconnectReference={onDisconnectReference} onStartReferenceSelection={onStartReferenceSelection} />}
          {task.kind === 'text' && <p className="sr-only" data-testid="text-service-status">{textServiceLabel}</p>}
          {isImage && <p className="sr-only" data-testid="image-service-status" role="status">{selectedProfile?.available ? `每次生成 1 张，最多引用 ${capabilities?.maxReferences ?? 0} 张图片` : selectedProfile?.unavailableReason ?? '正在读取图片模型配置'}</p>}
          {task.id === 'join' && <p className="canvas-config-note" data-testid="join-service-status">素材混剪</p>}
          {task.id === 'join' && joinDuration && <p className="canvas-config-note" data-testid="join-duration-summary" data-duration-seconds={joinDuration.seconds ?? ''}>已连接 {joinDuration.count} 段视频 · {joinDuration.seconds === null ? '部分片段时长等待确定' : `${joinDuration.estimated ? '按当前生成参数预计' : '实际总时长'} ${Number(joinDuration.seconds.toFixed(3))} 秒`}</p>}
          {inputProblem && !(openMenu === 'references' && maxReferenceImages > 0) && <p className={waitingForFirstFrame ? 'canvas-config-note' : 'sr-only'} data-testid="generation-input-problem" role="status">{inputProblem}</p>}
        </div>
        {(isVideo || isImage) && <div className="fmv-media-count" aria-label="总字数">总字数 {textCount}</div>}
        <footer className="canvas-config-footer" aria-label="生成参数">
          {task.id !== 'join' && <GeneratorMenu label="模型" testId="generation-model-trigger" open={openMenu === 'model'} onToggle={() => toggleMenu('model')} width="small" trigger={<span>{selectedProfile?.label || model || '模型'}</span>}>
            <div role="listbox" aria-label="模型">
              {models.map((item) => <button key={item.id} type="button" role="option" aria-selected={item.id === selectedProfile?.id} className="canvas-config-option" data-testid={`generation-model-${item.id}`} title={item.unavailableReason ?? undefined} disabled={!item.available} onClick={() => selectModel(item)}>{item.label}</button>)}
            </div>
            {!selectedProfile?.available && <Link className="canvas-config-option" href="/zh-CN/settings/models">模型设置</Link>}
          </GeneratorMenu>}
          {isVideo && <GeneratorMenu label="视频输入" testId="video-input-trigger" open={openMenu === 'mode'} onToggle={() => toggleMenu('mode')} width="small" trigger={<span>{activeVideoOption?.label ?? videoSourceLabels[videoMode]}</span>}>
            <div role="listbox" aria-label="视频输入">
              {videoOptions.map(({ id, mode, label, disabled, reason }) => <button key={id} type="button" role="option" aria-selected={activeVideoOption?.id === id} className="canvas-config-option" data-testid={`video-mode-${id}`} disabled={disabled} title={reason} onClick={() => { if (mode) selectVideoMode(id, mode); }}>{label}</button>)}
            </div>
          </GeneratorMenu>}
          {(isImage || isVideo) && <GeneratorMenu label="生成规格" testId="generation-specifications-trigger" open={openMenu === 'specifications'} onToggle={() => toggleMenu('specifications')} width="wide" trigger={<span>{isVideo ? videoSpec : imageSpec}</span>}>
            {isVideo ? <CanvasVideoSettings node={node} model={model} onConfigChange={onConfigChange} resolutions={capabilities?.resolutions} durationRange={durationRange} ratios={canvasVideoRatios(node.metadata, selectedProfile)} ratio={videoRatio} inline /> : <CanvasImageSpecs metadata={node.metadata} profile={selectedProfile} update={(patch) => onConfigChange(node.id, patch)} />}
          </GeneratorMenu>}
          {isVideo && capabilities?.audio && <button type="button" title="生成声音" aria-label="生成声音" aria-pressed={node.metadata?.generateAudio !== 'false'} onClick={() => onConfigChange(node.id, { generateAudio: node.metadata?.generateAudio === 'false' ? 'true' : 'false' })}>{node.metadata?.generateAudio === 'false' ? <VolumeX size={17} /> : <Volume2 size={17} />}</button>}
          <button type="button" aria-label={actionLabel} title={inputProblem || actionLabel} className="canvas-config-action" data-testid={actionId} disabled={busy || !canGenerate || (task.kind === 'text' && !textConnected) || (isImage && (!selectedProfile?.available || !imageSize))} onClick={() => onGenerate(node.id)}><ArrowUp size={18} aria-hidden="true" /></button>
        </footer>
      </div>
    </div>
  );
}

export const CanvasConfigNodePanel = memo(CanvasConfigNodePanelView);
