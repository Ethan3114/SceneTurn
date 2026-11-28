import React, { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import { ChevronRight, Copy, Download, FileText, Group, Image as ImageIcon, MoreHorizontal, Music2, Pencil, Plus, Puzzle, RefreshCw, Settings2, Star, Trash2, Video } from "lucide-react";

import { canvasThemes } from "./canvas-theme";
import { CANVAS_PORT_OFFSET } from "./canvas-constants";
import { formatBytes } from "./image-utils";
import { pickImageSource } from "./image-thumbnail";
import { previewUrlFor, subscribeImagePreviews, getImagePreviewRevision } from "../media";
import { getNodeDefinition } from "./node-registry";
import { buildNodeContext } from "./plugin-node-context";
import { useThemeStore } from "./use-theme-store";
import { CanvasResourceMentionTextarea } from "./canvas-resource-mention-textarea";
import { CanvasTextShortcuts, type TextShortcut } from "../CanvasTextShortcuts";
import { CanvasNodeType, type CanvasNodeData, type CanvasNodeImage, type CanvasNodeText, type Position } from "./canvas";
import type { CanvasNodeContext, CanvasPluginHost } from "./canvas-plugin";
import type { CanvasResourceReference } from "./canvas-resource-references";
import { useTranslation } from "react-i18next";
import "./canvas-node-card.css";

type ResizeCorner = "top-left" | "top-right" | "bottom-left" | "bottom-right";
const selectionBlue = "#A9BAFF";
const nodeIcons = {
    [CanvasNodeType.Text]: FileText, [CanvasNodeType.Image]: ImageIcon, [CanvasNodeType.Video]: Video,
    [CanvasNodeType.Audio]: Music2, [CanvasNodeType.Config]: Settings2, [CanvasNodeType.Group]: Group,
};

type CanvasNodeProps = {
    data: CanvasNodeData;
    result?: CanvasNodeData;
    resultVersions?: ReactNode;
    canLinkOutput?: boolean;
    scale: number;
    isSelected: boolean;
    isRelated: boolean;
    isFocusRelated: boolean;
    isConnectionTarget: boolean;
    isConnecting: boolean;
    referenceSelectionState?: "target" | "disabled" | "available";
    showPanel: boolean;
    statusLabel?: string;
    panelWidth?: number;
    panelOffsetX?: number;
    panelMaxHeight?: number;
    showImageInfo: boolean;
    videoInfo?: { width: number; height: number; seconds: number };
    mentionReferences?: CanvasResourceReference[];
    pluginHost?: CanvasPluginHost;
    registryVersion?: number;
    renderPanel?: (node: CanvasNodeData) => ReactNode;
    resultTools?: ReactNode;
    renderMenuItems?: (node: CanvasNodeData) => ReactNode;
    renderNodeContent?: (node: CanvasNodeData) => ReactNode;
    onTextShortcut?: (node: CanvasNodeData, action: Exclude<TextShortcut, 'write'>) => void;
    groupChildCount?: number;
    isGroupDropTarget?: boolean;
    batchExpanded?: boolean;
    onSelect: (nodeId: string, additive: boolean) => void;
    onDelete?: (nodeId: string) => void;
    deleteDisabled?: boolean;
    onHoverStart: (nodeId: string) => void;
    onHoverEnd: (nodeId: string) => void;
    onConnectStart: (event: React.MouseEvent, nodeId: string, handleType: "source" | "target") => void;
    onResizeStart: (nodeId: string) => void;
    onResize: (nodeId: string, width: number, height: number, position?: Position) => void;
    onResizeEnd: (nodeId: string) => void;
    onContentChange: (nodeId: string, content: string) => void;
    onTitleChange: (nodeId: string, title: string) => void;
    onToggleBatch?: (nodeId: string) => void;
    onSetBatchPrimary?: (nodeId: string, itemId: string) => void;
    onDuplicateBatchImage?: (node: CanvasNodeData, imageId: string) => void;
    onDownloadBatchImage?: (node: CanvasNodeData, imageId: string) => void;
    onRetryBatchImage?: (node: CanvasNodeData, imageId: string) => void;
    onDeleteBatchImage?: (nodeId: string, imageId: string) => void;
    onRetry?: (node: CanvasNodeData) => void;
    onViewImage?: (node: CanvasNodeData, imageId?: string) => void;
    onContextMenu: (event: React.MouseEvent, nodeId: string) => void;
};

type NodeContentRendererProps = {
    node: CanvasNodeData;
    theme: (typeof canvasThemes)[keyof typeof canvasThemes];
    statusLabel?: string;
    isEditingContent: boolean;
    textareaRef: React.RefObject<HTMLTextAreaElement | null>;
    isBatchRoot: boolean;
    batchCount: number;
    batchExpanded: boolean;
    scale: number;
    renderNodeContent?: (node: CanvasNodeData) => ReactNode;
    pluginContext?: CanvasNodeContext | null;
    onContentChange: (nodeId: string, content: string) => void;
    onStopEditing: () => void;
    onTextShortcut?: (action: TextShortcut) => void;
    mentionReferences: CanvasResourceReference[];
    onRetry?: (node: CanvasNodeData) => void;
    onToggleBatch?: () => void;
    onSetBatchPrimary?: (itemId: string) => void;
    onDuplicateBatchImage?: (imageId: string) => void;
    onDownloadBatchImage?: (imageId: string) => void;
    onRetryBatchImage?: (imageId: string) => void;
    onDeleteBatchImage?: (imageId: string) => void;
    onViewBatchImage?: (imageId: string) => void;
};

export const CanvasNode = React.memo(function CanvasNode({
    data,
    result,
    resultVersions,
    canLinkOutput,
    scale,
    isSelected,
    isRelated,
    isFocusRelated,
    isConnectionTarget,
    isConnecting,
    referenceSelectionState,
    showPanel,
    statusLabel,
    panelWidth = 600,
    panelOffsetX = 0,
    panelMaxHeight = 400,
    showImageInfo,
    videoInfo,
    mentionReferences = [],
    pluginHost,
    renderPanel,
    resultTools,
    renderMenuItems,
    renderNodeContent,
    onTextShortcut,
    groupChildCount = 0,
    isGroupDropTarget = false,
    batchExpanded = false,
    onSelect,
    onDelete,
    deleteDisabled = false,
    onHoverStart,
    onHoverEnd,
    onConnectStart,
    onResizeStart,
    onResize,
    onResizeEnd,
    onContentChange,
    onTitleChange,
    onToggleBatch,
    onSetBatchPrimary,
    onDuplicateBatchImage,
    onDownloadBatchImage,
    onRetryBatchImage,
    onDeleteBatchImage,
    onRetry,
    onViewImage,
    onContextMenu,
}: CanvasNodeProps) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    const [hovered, setHovered] = useState(false);
    const definition = getNodeDefinition(data.type);
    const pluginContext = useMemo<CanvasNodeContext | null>(() => (pluginHost ? buildNodeContext(pluginHost, data, theme, scale, isSelected) : null), [pluginHost, data, theme, scale, isSelected]);
    const [isEditingContent, setIsEditingContent] = useState(false);
    const [isEditingTitle, setIsEditingTitle] = useState(false);
    const [titleDraft, setTitleDraft] = useState(data.title || "");
    const contentData = result ?? data;
    const hasImageContent = contentData.type === CanvasNodeType.Image && Boolean(contentData.metadata?.content);
    const hasVideoContent = contentData.type === CanvasNodeType.Video && Boolean(contentData.metadata?.content);
    const hasAudioContent = contentData.type === CanvasNodeType.Audio && Boolean(contentData.metadata?.content);
    const isGroup = data.type === CanvasNodeType.Group;
    const batchCount = contentData.type === CanvasNodeType.Image ? contentData.metadata?.images?.length || 0 : contentData.type === CanvasNodeType.Text ? contentData.metadata?.texts?.length || 0 : 0;
    const isBatchRoot = batchCount > 1;
    // Nodes with the interaction/move toggle ignore content pointer events in move mode and allow interaction in interactive mode.
    // forceInteractive states such as editing stay interactive, as do empty nodes so their upload and generation actions remain usable.
    const supportsInteractionToggle = Boolean(definition?.interactionToggle);
    const forceInteractive = supportsInteractionToggle ? Boolean(definition?.forceInteractive?.(data)) : false;
    const contentInteractive = !supportsInteractionToggle || forceInteractive || !data.metadata?.content ? true : Boolean(data.metadata?.interactive);
    // Transparent nodes such as SVGs blend into the canvas while retaining outlines for selected or related states.
    const transparentBg = Boolean(definition?.transparentBackground);
    const isActive = isConnectionTarget || isSelected || isFocusRelated;
    const NodeIcon = nodeIcons[contentData.type as CanvasNodeType] || Puzzle;
    const showMediaInfo = (showImageInfo && hasImageContent) || hasVideoContent || hasAudioContent;
    const menuRef = useRef<HTMLDetailsElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const titleInputRef = useRef<HTMLInputElement>(null);
    const resizeRef = useRef({
        isResizing: false,
        corner: "bottom-right" as ResizeCorner,
        startX: 0,
        startY: 0,
        startLeft: 0,
        startTop: 0,
        startWidth: 0,
        startHeight: 0,
        keepRatio: false,
        ratio: 1,
    });

    useEffect(() => {
        setTitleDraft(data.title || "");
    }, [data.title]);

    useEffect(() => {
        if (!isEditingTitle) return;
        titleInputRef.current?.focus();
        titleInputRef.current?.select();
    }, [isEditingTitle]);

    const finishTitleEditing = useCallback(() => {
        const title = titleDraft.trim() || data.title || t("canvas.node.untitled");
        setTitleDraft(title);
        setIsEditingTitle(false);
        if (title !== data.title) onTitleChange(data.id, title);
    }, [data.id, data.title, onTitleChange, t, titleDraft, setTitleDraft, setIsEditingTitle]);

    useEffect(() => {
        if (!isEditingTitle) return;
        const handleOutsidePointerDown = (event: PointerEvent) => {
            const target = event.target;
            if (target instanceof Node && titleInputRef.current?.contains(target)) return;
            finishTitleEditing();
        };
        window.addEventListener("pointerdown", handleOutsidePointerDown, true);
        return () => window.removeEventListener("pointerdown", handleOutsidePointerDown, true);
    }, [finishTitleEditing, isEditingTitle]);

    useEffect(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;

        const handleWheel = (event: WheelEvent) => event.stopPropagation();
        textarea.addEventListener("wheel", handleWheel, { passive: false });
        return () => textarea.removeEventListener("wheel", handleWheel);
    }, [data.type, isEditingContent]);

    useEffect(() => {
        if (!isEditingContent) return;
        const textarea = textareaRef.current;
        textarea?.focus();
        textarea?.setSelectionRange(textarea.value.length, textarea.value.length);
    }, [isEditingContent]);

    useEffect(() => {
        if (!isEditingContent) return;

        const handleOutsidePointerDown = (event: PointerEvent) => {
            const target = event.target;
            if (!(target instanceof Node)) return;
            if (isEditingContent && textareaRef.current?.contains(target)) return;

            setIsEditingContent(false);
        };

        window.addEventListener("pointerdown", handleOutsidePointerDown, true);
        return () => window.removeEventListener("pointerdown", handleOutsidePointerDown, true);
    }, [isEditingContent]);

    const handleResizeMove = useCallback(
        (event: MouseEvent) => {
            if (!resizeRef.current.isResizing) return;

            const dx = (event.clientX - resizeRef.current.startX) / scale;
            const dy = (event.clientY - resizeRef.current.startY) / scale;
            const minWidth = 220;
            const minHeight = 160;
            const startRight = resizeRef.current.startLeft + resizeRef.current.startWidth;
            const startBottom = resizeRef.current.startTop + resizeRef.current.startHeight;
            const fromLeft = resizeRef.current.corner.includes("left");
            const fromTop = resizeRef.current.corner.includes("top");
            const rawWidth = Math.max(minWidth, resizeRef.current.startWidth + (fromLeft ? -dx : dx));
            const rawHeight = Math.max(minHeight, resizeRef.current.startHeight + (fromTop ? -dy : dy));
            let width = rawWidth;
            let height = rawHeight;
            if (resizeRef.current.keepRatio) {
                const ratio = resizeRef.current.ratio;
                if (Math.abs(dx) >= Math.abs(dy)) {
                    height = width / ratio;
                } else {
                    width = height * ratio;
                }
                if (height < minHeight) {
                    height = minHeight;
                    width = height * ratio;
                }
                if (width < minWidth) {
                    width = minWidth;
                    height = width / ratio;
                }
            }

            onResize(data.id, width, height, {
                x: fromLeft ? startRight - width : resizeRef.current.startLeft,
                y: fromTop ? startBottom - height : resizeRef.current.startTop,
            });
        },
        [data.id, onResize, scale],
    );

    const handleResizeUp = useCallback(() => {
        resizeRef.current.isResizing = false;
        window.removeEventListener("mousemove", handleResizeMove);
        window.removeEventListener("mouseup", handleResizeUp);
        onResizeEnd(data.id);
    }, [data.id, handleResizeMove, onResizeEnd]);

    const handleResizeMouseDown = (event: React.MouseEvent, corner: ResizeCorner) => {
        event.stopPropagation();
        event.preventDefault();
        onResizeStart(data.id);
        resizeRef.current = {
            isResizing: true,
            corner,
            startX: event.clientX,
            startY: event.clientY,
            startLeft: data.position.x,
            startTop: data.position.y,
            startWidth: data.width,
            startHeight: data.height,
            keepRatio: (data.type === CanvasNodeType.Image && !data.metadata?.freeResize) || data.type === CanvasNodeType.Video || Boolean(definition?.keepAspectRatio?.(data)),
            ratio: (data.metadata?.naturalWidth || data.width) / (data.metadata?.naturalHeight || data.height || 1),
        };
        window.addEventListener("mousemove", handleResizeMove);
        window.addEventListener("mouseup", handleResizeUp);
    };

    useEffect(() => {
        return () => {
            window.removeEventListener("mousemove", handleResizeMove);
            window.removeEventListener("mouseup", handleResizeUp);
        };
    }, [handleResizeMove, handleResizeUp]);

    return (
        <div
            data-node-id={data.id}
            data-testid="canvas-node"
            data-selected={isSelected}
            data-node-type={data.type}
            tabIndex={0}
            role="group"
            aria-label={data.title || t("canvas.node.untitled")}
            className={`node-element group/node absolute flex select-none flex-col transition-shadow duration-200 ${isGroup ? "z-[5]" : isSelected ? "z-50" : "z-10"} ${referenceSelectionState === "available" ? "cursor-pointer" : referenceSelectionState ? "cursor-not-allowed" : ""}`}
            style={{
                transform: `translate(${data.position.x}px, ${data.position.y}px)`,
                width: data.width,
                height: data.height,
                transition: "box-shadow 200ms ease",
                contain: "layout style",
                "--fmv-node-surface": theme.node.fill,
                "--fmv-node-header": theme.node.panel,
                "--fmv-node-field": theme.canvas.background,
                "--fmv-node-divider": theme.toolbar.border,
                "--fmv-node-border": theme.node.stroke,
                "--fmv-node-text": theme.node.text,
                "--fmv-node-muted": theme.node.muted,
                "--fmv-node-accent": theme.node.activeStroke,
                "--fmv-node-port-ring": theme.node.port,
                "--fmv-node-port-offset": `${CANVAS_PORT_OFFSET}px`,
            } as React.CSSProperties}
            onMouseEnter={() => {
                setHovered(true);
                onHoverStart(data.id);
            }}
            onMouseLeave={() => {
                setHovered(false);
                onHoverEnd(data.id);
            }}
            onContextMenu={(event) => {
                if (referenceSelectionState) event.preventDefault();
                else {
                    onContextMenu(event, data.id);
                    if (menuRef.current) {
                        menuRef.current.open = true;
                        menuRef.current.querySelector<HTMLElement>("summary")?.focus({ preventScroll: true });
                    }
                }
            }}
        >
            <div
                data-testid="canvas-node-body"
                data-canvas-drag-node={data.id}
                className="fmv-node-card"
                data-highlight={isActive || isGroupDropTarget ? "active" : isRelated ? "related" : undefined}
                style={{ background: isGroup || transparentBg ? "transparent" : undefined, borderStyle: isGroup ? "dashed" : "solid" }}
                onPointerDownCapture={(event) => {
                    const target = event.target instanceof Element ? event.target : null;
                    if (event.button !== 0 || !target || !event.currentTarget.contains(target)) return;
                    if (target.closest('summary,button:not([data-canvas-node-title]),input,textarea,select,a,video,audio')) {
                        if (!referenceSelectionState) onSelect(data.id, false);
                    }
                }}
                onDragStart={(event) => { if (event.target instanceof HTMLImageElement) event.preventDefault(); }}
                onDoubleClick={(event) => {
                    if (referenceSelectionState) {
                        event.stopPropagation();
                        return;
                    }
                    if (definition?.onDoubleClick && pluginContext) {
                        if (definition.onDoubleClick(pluginContext)) event.stopPropagation();
                        return;
                    }
                    if (hasImageContent) {
                        event.stopPropagation();
                        onViewImage?.(contentData);
                        return;
                    }
                    if (contentData.type !== CanvasNodeType.Text || contentData.metadata?.publicMaterialRole || contentData.metadata?.pendingResult) return;
                    event.stopPropagation();
                    setIsEditingContent(true);
                }}
            >
                <div data-testid="canvas-node-header" className="fmv-node-card-header" onDoubleClick={(event) => event.stopPropagation()}>
                    <NodeIcon aria-hidden="true" size={15} />
                    {isEditingTitle ? (
                        <input
                            ref={titleInputRef}
                            aria-label="节点名称"
                            value={titleDraft}
                            maxLength={64}
                            className="fmv-node-title-edit"
                            onMouseDown={(event) => event.stopPropagation()}
                            onChange={(event) => setTitleDraft(event.target.value)}
                            onBlur={finishTitleEditing}
                            onKeyDown={(event) => {
                                if (event.key === "Enter") finishTitleEditing();
                                if (event.key === "Escape") {
                                    setTitleDraft(data.title || "");
                                    setIsEditingTitle(false);
                                }
                            }}
                        />
                    ) : (
                        <button
                            type="button"
                            data-testid="canvas-node-title"
                            data-canvas-node-title
                            className="fmv-node-title"
                            title={data.title || t("canvas.node.untitled")}
                            onClick={(event) => { if (event.detail === 0) onSelect(data.id, false); }}
                            onDoubleClick={(event) => {
                                event.stopPropagation();
                                setIsEditingTitle(true);
                            }}
                        >
                            {data.title || t("canvas.node.untitled")}
                        </button>
                    )}
                    {statusLabel && <span className="fmv-node-card-status" role="status" title={statusLabel}>{statusLabel}</span>}
                    {isGroup && <span className="fmv-node-card-status">{t("canvas.node.nodeCount", { count: groupChildCount })}</span>}
                    {showMediaInfo && <MediaInfoBar node={contentData} videoInfo={videoInfo} />}
                    {resultVersions}
                    {!referenceSelectionState && <details ref={menuRef} className="fmv-node-card-menu" data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()}
                        onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }}
                        onKeyDown={(event) => {
                            if (event.key !== "Escape") return;
                            event.stopPropagation();
                            event.currentTarget.open = false;
                            event.currentTarget.querySelector<HTMLElement>("summary")?.focus();
                        }}>
                        <summary data-testid="canvas-node-more" aria-label="节点操作" title="节点操作"><MoreHorizontal size={17} /></summary>
                        <div className="fmv-node-card-menu-popover">
                            {renderMenuItems && <div className="fmv-node-menu-items" onClick={() => { if (menuRef.current) menuRef.current.open = false; }}>{renderMenuItems(data)}</div>}
                            <button type="button" onClick={() => { if (menuRef.current) menuRef.current.open = false; setIsEditingTitle(true); }}><Pencil size={14} />重命名</button>
                            {onDelete && <button type="button" data-testid="canvas-node-delete" aria-label={`删除节点：${data.title || t("canvas.node.untitled")}`}
                                disabled={deleteDisabled} onClick={() => { if (menuRef.current) menuRef.current.open = false; onDelete(data.id); }}>
                                <Trash2 size={14} />删除节点
                            </button>}
                        </div>
                    </details>}
                </div>
                <div
                    className={`fmv-node-card-content ${isBatchRoot ? "overflow-visible" : "overflow-hidden"}`}
                    style={
                        {
                            pointerEvents: contentInteractive ? undefined : "none",
                        } as React.CSSProperties
                    }
                >
                    <NodeContent
                        key={contentData.id}
                        statusLabel={statusLabel}
                        node={contentData}
                        theme={theme}
                        isEditingContent={isEditingContent}
                        textareaRef={textareaRef}
                        isBatchRoot={isBatchRoot}
                        batchCount={batchCount}
                        batchExpanded={batchExpanded}
                        scale={scale}
                        renderNodeContent={result ? undefined : renderNodeContent}
                        pluginContext={pluginContext}
                        mentionReferences={mentionReferences}
                        onContentChange={onContentChange}
                        onStopEditing={() => setIsEditingContent(false)}
                        onTextShortcut={onTextShortcut && !result ? (action) => {
                            if (action === 'write') setIsEditingContent(true);
                            else onTextShortcut(data, action);
                        } : undefined}
                        onRetry={onRetry}
                        onToggleBatch={() => onToggleBatch?.(data.id)}
                        onSetBatchPrimary={(itemId) => onSetBatchPrimary?.(contentData.id, itemId)}
                        onDuplicateBatchImage={(imageId) => onDuplicateBatchImage?.(contentData, imageId)}
                        onDownloadBatchImage={(imageId) => onDownloadBatchImage?.(contentData, imageId)}
                        onRetryBatchImage={onRetryBatchImage ? (imageId) => onRetryBatchImage(contentData, imageId) : undefined}
                        onDeleteBatchImage={(imageId) => onDeleteBatchImage?.(contentData.id, imageId)}
                        onViewBatchImage={(imageId) => onViewImage?.(contentData, imageId)}
                    />
                </div>

                {referenceSelectionState && (referenceSelectionState !== "available" || hovered) ? (
                    <div className="pointer-events-none absolute inset-0 z-[60] grid place-items-center rounded-[inherit]" style={{ background: `color-mix(in srgb, ${theme.canvas.background} ${referenceSelectionState === "target" ? 78 : referenceSelectionState === "disabled" ? 60 : 34}%, transparent)`, boxShadow: referenceSelectionState === "available" ? `inset 0 0 0 2px ${selectionBlue}` : undefined }}>
                        {referenceSelectionState !== "disabled" ? <span className="rounded-lg px-3 py-2 text-sm font-medium shadow-sm" style={{ background: theme.toolbar.panel, color: theme.node.text }}>{t(referenceSelectionState === "target" ? "canvas.references.selecting" : "canvas.references.choose")}</span> : null}
                    </div>
                ) : null}

                {!referenceSelectionState ? <ResizeHandle corner="top-left" onMouseDown={handleResizeMouseDown} /> : null}
                {!referenceSelectionState ? <ResizeHandle corner="top-right" onMouseDown={handleResizeMouseDown} /> : null}
                {!referenceSelectionState ? <ResizeHandle corner="bottom-left" onMouseDown={handleResizeMouseDown} /> : null}
                {!referenceSelectionState ? <ResizeHandle corner="bottom-right" onMouseDown={handleResizeMouseDown} /> : null}
            </div>

            {!referenceSelectionState && !isGroup ? <ConnectionHandleDot nodeId={data.id} side="left" active={isConnectionTarget || isSelected || isConnecting && hovered} onMouseDown={(event) => onConnectStart(event, data.id, "target")} /> : null}
            {!referenceSelectionState && !isGroup && (canLinkOutput ?? ((definition?.hasSourceHandle ?? true) && data.type !== CanvasNodeType.Config)) ? <ConnectionHandleDot nodeId={data.id} side="right" active={isSelected || hovered} onMouseDown={(event) => onConnectStart(event, data.id, "source")} /> : null}

            {showPanel && resultTools && <div className="fmv-result-tools-anchor" style={{ marginLeft: panelOffsetX, transform: `translateX(-50%) scale(${1 / scale})` }}>{resultTools}</div>}
            {/* Counter-scale the panel so its on-screen size stays constant while the canvas zooms. */}
            {showPanel && !isGroup && renderPanel ? (
                <div className="fmv-attached-node-panel absolute left-1/2 top-full z-[70] pt-4" style={{ width: panelWidth, maxHeight: panelMaxHeight, marginLeft: panelOffsetX, transform: `translateX(-50%) scale(${1 / scale})`, transformOrigin: "top center" }} data-node-panel={data.id}>
                    {renderPanel(data)}
                </div>
            ) : null}
        </div>
    );
});

function NodeContent(props: NodeContentRendererProps) {
    if ((props.node.type === CanvasNodeType.Config || props.node.metadata?.scriptRows) && props.renderNodeContent) return props.renderNodeContent(props.node);
    if (props.isBatchRoot && props.node.type === CanvasNodeType.Image) return <ImageNodeContent {...props} />;
    if (props.node.type === CanvasNodeType.Text && props.node.metadata?.texts?.length && (props.node.metadata.status !== "error" || props.node.metadata.texts.some((text) => text.content))) return <TextContent {...props} />;
    if (props.node.metadata?.status === "loading") return <LoadingContent theme={props.theme} statusLabel={props.statusLabel} />;
    if (props.node.metadata?.status === "error") return <ErrorContent node={props.node} theme={props.theme} onRetry={props.onRetry} />;

    const Renderer = nodeContentRenderers[props.node.type as CanvasNodeType];
    if (Renderer) return <Renderer {...props} />;

    // Render plugin nodes with their registered renderer, or show the missing-plugin placeholder.
    const definition = getNodeDefinition(props.node.type);
    if (definition?.Content && props.pluginContext) {
        const PluginContent = definition.Content;
        return <PluginContent ctx={props.pluginContext} />;
    }
    return <MissingPluginContent theme={props.theme} type={props.node.type} />;
}

const nodeContentRenderers = {
    [CanvasNodeType.Text]: TextContent,
    [CanvasNodeType.Image]: ImageNodeContent,
    [CanvasNodeType.Config]: EmptyImageContent,
    [CanvasNodeType.Video]: VideoNodeContent,
    [CanvasNodeType.Audio]: AudioNodeContent,
    [CanvasNodeType.Group]: () => null,
} satisfies Record<CanvasNodeType, (props: NodeContentRendererProps) => ReactNode>;

function LoadingContent({ theme, statusLabel }: Pick<NodeContentRendererProps, "theme" | "statusLabel">) {
    const { t } = useTranslation();
    return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3" style={{ color: theme.node.activeStroke }}>
            <div className="size-10 animate-spin rounded-full border-2" style={{ borderColor: theme.node.stroke, borderTopColor: theme.node.activeStroke }} />
            <span className="text-xs">{statusLabel || t("canvas.node.generating")}</span>
        </div>
    );
}

function ErrorContent({ node, theme, onRetry }: Pick<NodeContentRendererProps, "node" | "theme" | "onRetry">) {
    const { t } = useTranslation();
    return (
        <div className="flex max-w-[260px] flex-col items-center gap-3 px-5 text-center">
            <div className="text-xs leading-5 text-red-300">{node.metadata?.errorDetails || t("canvas.node.failed")}</div>
            <button
                type="button"
                disabled={!onRetry}
                className="inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition hover:scale-[1.02]"
                style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.node.text }}
                onClick={(event) => {
                    event.stopPropagation();
                    onRetry?.(node);
                }}
                onMouseDown={(event) => event.stopPropagation()}
            >
                <RefreshCw className="size-3.5" />
                {t("canvas.node.retry")}
            </button>
        </div>
    );
}

function MissingPluginContent({ theme, type }: Pick<NodeContentRendererProps, "theme"> & { type: string }) {
    const { t } = useTranslation();
    return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 px-4 text-center" style={{ color: theme.node.placeholder }}>
            <Puzzle className="size-7 opacity-40" />
            <span className="text-sm">{t("canvas.node.missingPlugin")}</span>
            <span className="text-[11px] opacity-70">{t("canvas.node.missingPluginDescription", { type })}</span>
        </div>
    );
}

function TextContent({ node, theme, isEditingContent, textareaRef, mentionReferences, batchExpanded, onContentChange, onStopEditing, onTextShortcut, onToggleBatch, onSetBatchPrimary }: NodeContentRendererProps) {
    const { t } = useTranslation();
    const fontSize = node.metadata?.fontSize || 14;
    const textStyle = { fontSize: `${fontSize}px`, lineHeight: `${Math.round(fontSize * 1.65)}px`, color: theme.node.text, boxSizing: "border-box" } as React.CSSProperties;
    const texts = node.metadata?.texts || [];
    const batchCount = texts.length;
    const isBatchRoot = batchCount > 1;
    const primaryTextId = node.metadata?.primaryTextId || texts[0]?.id;
    const primaryText = texts.find((text) => text.id === primaryTextId);
    const content = primaryText?.content || node.metadata?.content || "";
    const paddingClass = isBatchRoot ? "px-4 pb-4 pt-14" : "p-4";

    if (onTextShortcut && !isEditingContent && !content && !batchCount && !node.metadata?.publicMaterialRole && !node.metadata?.pendingResult) {
        return <CanvasTextShortcuts onChoose={onTextShortcut} />;
    }

    return (
        <BatchFrame batchCount={batchCount} batchExpanded={batchExpanded}>
            {batchExpanded
                ? texts
                      .filter((text) => text.id !== primaryTextId)
                      .map((text, index) => <ExpandedTextCard key={text.id} node={node} text={text} index={index} onSetPrimary={() => onSetBatchPrimary?.(text.id)} />)
                : null}
            <div className="flex h-full w-full flex-col overflow-hidden">
                {isEditingContent && !node.metadata?.publicMaterialRole ? (
                    <CanvasResourceMentionTextarea
                        data-testid="canvas-text"
                        aria-label="创作文字"
                        ref={textareaRef}
                        className={`thin-scrollbar block h-full w-full resize-none overflow-y-auto whitespace-pre-wrap break-words border-none bg-transparent m-0 font-sans outline-none select-text appearance-none ${paddingClass}`}
                        style={textStyle}
                        value={content}
                        references={mentionReferences}
                        highlightLabels={false}
                        onChange={(value) => onContentChange(node.id, value)}
                        onBlur={onStopEditing}
                        onKeyDown={(event) => {
                            if (event.key === "Escape") onStopEditing();
                        }}
                        onMouseDown={(event) => event.stopPropagation()}
                        onPointerDown={(event) => event.stopPropagation()}
                        onWheel={(event) => event.stopPropagation()}
                    />
                ) : content ? (
                    <div className={`thin-scrollbar block h-full w-full overflow-y-auto whitespace-pre-wrap break-words bg-transparent font-sans ${paddingClass}`} style={textStyle} onWheel={(event) => event.stopPropagation()}>
                        {content}
                    </div>
                ) : primaryText ? (
                    <TextSlotStatus text={primaryText} />
                ) : (
                    <div className="p-4 font-sans" style={{ color: theme.node.placeholder }}>
                        {node.metadata?.publicMaterialRole ? '在岗位区域查看成果，通过“问问 AI”提出修改。' : node.metadata?.pendingResult ? t("canvas.node.pendingResult") : t("canvas.node.editText")}
                    </div>
                )}
            </div>
            {isBatchRoot ? (
                <button
                    type="button"
                    className="absolute right-2.5 top-2.5 z-30 flex h-8 items-center justify-center gap-1.5 rounded-full border px-3 text-xs font-semibold shadow-[0_6px_18px_rgba(28,25,23,.12)] backdrop-blur-md transition hover:scale-[1.02]"
                    style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }}
                    aria-label={batchExpanded ? t("canvas.node.textBatchExpanded") : t("canvas.node.textBatchCollapsed")}
                    onClick={(event) => {
                        event.stopPropagation();
                        onToggleBatch?.();
                    }}
                    onMouseDown={(event) => event.stopPropagation()}
                    onPointerDown={(event) => event.stopPropagation()}
                >
                    <span className="leading-none">{t("canvas.controls.texts", { count: batchCount })}</span>
                    <ChevronRight className={`size-3.5 opacity-80 transition-transform ${batchExpanded ? "rotate-90" : ""}`} />
                </button>
            ) : null}
        </BatchFrame>
    );
}

function ExpandedTextCard({ node, text, index, onSetPrimary }: { node: CanvasNodeData; text: CanvasNodeText; index: number; onSetPrimary: () => void }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    const count = node.metadata?.texts?.length || 0;
    const columns = Math.min(count, 4);
    const rows = Math.ceil(count / columns);
    const rootSlot = (rows - 1) * columns;
    const slot = index >= rootSlot ? index + 1 : index;
    const x = (slot % columns) * (node.width + 18);
    const y = (Math.floor(slot / columns) - rows + 1) * (node.height + 18);

    return (
        <div
            className="absolute z-20 overflow-hidden rounded-3xl border shadow-[0_18px_50px_rgba(28,25,23,.14)]"
            style={
                {
                    left: x,
                    top: y,
                    width: node.width,
                    height: node.height,
                    background: theme.node.panel,
                    borderColor: theme.node.stroke,
                    "--batch-from-x": `${-x}px`,
                    "--batch-from-y": `${-y}px`,
                    "--batch-from-rotate": `${4 + index * 2}deg`,
                    animation: `canvas-batch-child-in 320ms ${index * 35}ms cubic-bezier(.2,.85,.18,1) both`,
                } as React.CSSProperties
            }
            onMouseDown={(event) => event.stopPropagation()}
            onPointerDown={(event) => event.stopPropagation()}
        >
            {text.content ? (
                <>
                    <div className="thin-scrollbar h-full overflow-y-auto whitespace-pre-wrap break-words px-4 pb-4 pt-14 font-mono text-sm leading-6" style={{ color: theme.node.text }} onWheel={(event) => event.stopPropagation()}>
                        {text.content}
                    </div>
                    <button type="button" className="pointer-events-none absolute right-2.5 top-2.5 flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium opacity-0 transition duration-150 hover:bg-black/5 group-hover/node:pointer-events-auto group-hover/node:opacity-100 dark:hover:bg-white/10" style={{ color: theme.node.text }} onClick={(event) => (event.stopPropagation(), onSetPrimary())}>
                        <Star className="size-3.5" style={{ color: selectionBlue }} />
                        {t("canvas.node.setPrimaryText")}
                    </button>
                </>
            ) : (
                <TextSlotStatus text={text} />
            )}
        </div>
    );
}

function TextSlotStatus({ text }: { text: CanvasNodeText }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    const failed = text.status === "error";
    const loading = text.status === "loading";
    return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-6 text-center" style={{ background: theme.node.fill, color: failed ? theme.node.text : theme.node.activeStroke }}>
            {failed ? <span className="text-xs leading-5">{text.errorDetails || t("canvas.node.failed")}</span> : loading ? <div className="size-10 animate-spin rounded-full border-2" style={{ borderColor: theme.node.stroke, borderTopColor: theme.node.activeStroke }} /> : <span className="text-xs">{t("apiErrors.noContent")}</span>}
            {loading ? <span className="text-[10px] tracking-[0.2em]">{t("canvas.node.generating")}</span> : null}
        </div>
    );
}

function ImageNodeContent(props: NodeContentRendererProps) {
    if (!props.node.metadata?.content && !props.isBatchRoot) return <EmptyImageContent {...props} />;

    return (
        <ImageContent
            node={props.node}
            batchExpanded={props.batchExpanded}
            scale={props.scale}
            onToggleBatch={props.onToggleBatch}
            onSetBatchPrimary={props.onSetBatchPrimary}
            onDuplicateBatchImage={props.onDuplicateBatchImage}
            onDownloadBatchImage={props.onDownloadBatchImage}
            onRetryBatchImage={props.onRetryBatchImage}
            onDeleteBatchImage={props.onDeleteBatchImage}
            onViewBatchImage={props.onViewBatchImage}
        />
    );
}

function EmptyImageContent({ node, theme }: NodeContentRendererProps) {
    const { t } = useTranslation();
    return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3" style={{ color: theme.node.placeholder }}>
            <div className="flex size-14 items-center justify-center rounded-2xl" style={{ background: theme.toolbar.activeBg }}>
                <ImageIcon className="size-6 opacity-30" />
            </div>
            <span className="text-[10px] tracking-[0.18em] opacity-50">{t(node.metadata?.pendingResult ? "canvas.node.pendingResult" : "canvas.node.emptyImage")}</span>
        </div>
    );
}

function VideoNodeContent({ node, theme }: NodeContentRendererProps) {
    const { t } = useTranslation();
    if (!node.metadata?.content)
        return (
            <div className="flex h-full w-full flex-col items-center justify-center gap-3" style={{ color: theme.node.placeholder }}>
                <Video className="size-7 opacity-35" />
                <span className="text-sm">{t(node.metadata?.pendingResult ? "canvas.node.pendingResult" : "canvas.node.emptyVideo")}</span>
            </div>
        );
    return <div className="relative h-full w-full">
        <video src={node.metadata.content} controls playsInline preload="metadata" className="h-full w-full bg-black object-contain" data-canvas-video={node.id} data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} />
        <div className="absolute inset-x-0 top-0 bottom-[48px]" aria-hidden="true" />
    </div>;
}

function AudioNodeContent({ node, theme }: NodeContentRendererProps) {
    const { t } = useTranslation();
    if (!node.metadata?.content)
        return (
            <div className="flex h-full w-full flex-col items-center justify-center gap-2" style={{ color: theme.node.placeholder }}>
                <Music2 className="size-7 opacity-35" />
                <span className="text-sm">{t("canvas.node.emptyAudio")}</span>
            </div>
        );
    return (
        <div className="flex h-full w-full flex-col justify-center gap-3 px-4" style={{ background: theme.node.fill, color: theme.node.text }}>
            <div className="flex min-w-0 items-center gap-2 text-sm opacity-70">
                <Music2 className="size-4 shrink-0" />
                <span className="truncate">{t("canvas.node.audio")}</span>
            </div>
            <audio src={node.metadata.content} controls className="w-full" data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} />
        </div>
    );
}

function ImageContent({
    node,
    batchExpanded,
    scale,
    onToggleBatch,
    onSetBatchPrimary,
    onDuplicateBatchImage,
    onDownloadBatchImage,
    onRetryBatchImage,
    onDeleteBatchImage,
    onViewBatchImage,
}: {
    node: CanvasNodeData;
    batchExpanded: boolean;
    scale: number;
    onToggleBatch?: () => void;
    onSetBatchPrimary?: (imageId: string) => void;
    onDuplicateBatchImage?: (imageId: string) => void;
    onDownloadBatchImage?: (imageId: string) => void;
    onRetryBatchImage?: (imageId: string) => void;
    onDeleteBatchImage?: (imageId: string) => void;
    onViewBatchImage?: (imageId: string) => void;
}) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    useSyncExternalStore(subscribeImagePreviews, getImagePreviewRevision);
    const images = node.metadata?.images || [];
    const batchCount = images.length;
    const isBatchRoot = batchCount > 1;
    const primaryImageId = node.metadata?.primaryImageId || images[0]?.id;
    const primaryImage = images.find((image) => image.id === primaryImageId);
    const primaryContent = primaryImage?.content || node.metadata?.content;
    const primaryPreviewUrl = previewUrlFor(primaryImage?.storageKey || node.metadata?.storageKey);
    const primarySource = primaryContent
        ? pickImageSource({
              previewUrl: primaryPreviewUrl,
              originalUrl: primaryContent,
              naturalWidth: primaryImage?.naturalWidth || node.metadata?.naturalWidth,
              naturalHeight: primaryImage?.naturalHeight || node.metadata?.naturalHeight,
              renderedWidth: node.width,
              renderedHeight: node.height,
              scale,
          })
        : undefined;

    return (
        <BatchFrame batchCount={batchCount} batchExpanded={batchExpanded}>
            {batchExpanded
                ? layoutBatchCards(images, primaryImageId, node.width, node.height).map((item) => (
                      <ExpandedImageCard
                          key={item.image.id}
                          node={node}
                          image={item.image}
                          index={item.slot}
                          frame={item}
                          scale={scale}
                          onView={() => onViewBatchImage?.(item.image.id)}
                          onSetPrimary={() => onSetBatchPrimary?.(item.image.id)}
                          onDuplicate={() => onDuplicateBatchImage?.(item.image.id)}
                          onDownload={() => onDownloadBatchImage?.(item.image.id)}
                          onRetry={onRetryBatchImage ? () => onRetryBatchImage(item.image.id) : undefined}
                          onDelete={() => onDeleteBatchImage?.(item.image.id)}
                      />
                  ))
                : null}
            <div className="h-full w-full overflow-hidden">
                {primaryContent ? (
                    <img
                        src={primarySource}
                        alt={node.title}
                        draggable={false}
                        onDragStart={(event) => event.preventDefault()}
                        className={`pointer-events-none block h-full w-full select-none ${node.metadata?.freeResize ? "object-fill" : "object-contain"}`}
                    />
                ) : (
                    <ImageSlotStatus image={primaryImage} />
                )}
            </div>
            {primaryImage?.status === "error" ? <BatchImageFailureActions placement="left" onRetry={onRetryBatchImage ? () => onRetryBatchImage(primaryImage.id) : undefined} onDelete={() => onDeleteBatchImage?.(primaryImage.id)} /> : null}
            {primaryImage?.content ? (
                <button type="button" className="pointer-events-none absolute left-2.5 top-2.5 z-30 flex h-8 items-center gap-1 rounded-lg border px-2 text-[10px] font-medium opacity-0 shadow-[0_6px_18px_rgba(15,23,42,.16)] backdrop-blur-md transition hover:scale-[1.02] group-hover/node:pointer-events-auto group-hover/node:opacity-100" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }} title={t("common.download")} onClick={(event) => (event.stopPropagation(), onDownloadBatchImage?.(primaryImage.id))}>
                    <Download className="size-3" />
                    {t("common.download")}
                </button>
            ) : null}
            {isBatchRoot ? (
                <button
                    type="button"
                    className="absolute right-2.5 top-2.5 z-30 flex h-8 items-center justify-center gap-1.5 rounded-full border px-3 text-xs font-semibold shadow-[0_6px_18px_rgba(28,25,23,.16)] backdrop-blur-md transition hover:scale-[1.02]"
                    style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }}
                    aria-label={batchExpanded ? t("canvas.node.batchExpanded") : t("canvas.node.batchCollapsed")}
                    onClick={(event) => {
                        event.stopPropagation();
                        onToggleBatch?.();
                    }}
                    onMouseDown={(event) => event.stopPropagation()}
                    onPointerDown={(event) => event.stopPropagation()}
                >
                    <span className="leading-none">{t("canvas.controls.images", { count: batchCount })}</span>
                    <ChevronRight className={`size-3.5 opacity-80 transition-transform ${batchExpanded ? "rotate-90" : ""}`} />
                </button>
            ) : null}
        </BatchFrame>
    );
}

function layoutBatchCards(images: CanvasNodeImage[], primaryId: string | undefined, frameWidth: number, frameHeight: number) {
    const count = images.length;
    const columns = Math.min(Math.max(count, 1), 4);
    const rows = Math.ceil(count / columns);
    const rootSlot = (rows - 1) * columns;
    const others = images.filter((image) => image.id !== primaryId);
    const slots: Array<CanvasNodeImage | null> = Array.from({ length: rows * columns }, () => null);
    let cursor = 0;
    for (let index = 0; index < slots.length; index += 1) {
        if (index === rootSlot) continue;
        slots[index] = others[cursor] || null;
        cursor += 1;
    }
    const heightOf = (image: CanvasNodeImage | null) => {
        const width = image?.naturalWidth || 0;
        const height = image?.naturalHeight || 0;
        if (!width || !height) return frameHeight;
        return Math.max(80, Math.round(frameWidth * (height / width)));
    };
    const rowHeights = Array.from({ length: rows }, (_, row) => {
        let max = row === rows - 1 ? frameHeight : 0;
        for (let column = 0; column < columns; column += 1) max = Math.max(max, heightOf(slots[row * columns + column]));
        return max || frameHeight;
    });
    const rowTops: number[] = [];
    let top = 0;
    for (let row = rows - 1; row >= 0; row -= 1) {
        rowTops[row] = top;
        top -= rowHeights[row] + 18;
    }
    return others.flatMap((image) => {
        const slot = slots.findIndex((item) => item?.id === image.id);
        if (slot < 0) return [];
        const row = Math.floor(slot / columns);
        const column = slot % columns;
        return [{ image, slot, left: column * (frameWidth + 18), top: rowTops[row], width: frameWidth, height: heightOf(image) }];
    });
}

function ExpandedImageCard({ node, image, index, frame, scale, onView, onSetPrimary, onDuplicate, onDownload, onRetry, onDelete }: { node: CanvasNodeData; image: CanvasNodeImage; index: number; frame?: { left: number; top: number; width: number; height: number }; scale: number; onView: () => void; onSetPrimary: () => void; onDuplicate: () => void; onDownload: () => void; onRetry?: () => void; onDelete: () => void }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    useSyncExternalStore(subscribeImagePreviews, getImagePreviewRevision);
    const count = node.metadata?.images?.length || 0;
    const columns = Math.min(count, 4);
    const rows = Math.ceil(count / columns);
    const rootSlot = (rows - 1) * columns;
    const slot = index >= rootSlot ? index + 1 : index;
    const column = slot % columns;
    const row = Math.floor(slot / columns);
    const x = frame ? frame.left : column * (node.width + 18);
    const y = frame ? frame.top : (row - rows + 1) * (node.height + 18);
    const cardWidth = frame?.width || node.width;
    const cardHeight = frame?.height || node.height;
    const source = image.content
        ? pickImageSource({
              previewUrl: previewUrlFor(image.storageKey),
              originalUrl: image.content,
              naturalWidth: image.naturalWidth,
              naturalHeight: image.naturalHeight,
              renderedWidth: cardWidth,
              renderedHeight: cardHeight,
              scale,
          })
        : undefined;

    return (
        <div
            className={`absolute z-20 overflow-hidden rounded-3xl ${image.content ? "" : "border shadow-[0_18px_50px_rgba(28,25,23,.18)]"}`}
            style={
                {
                    left: x,
                    top: y,
                    width: cardWidth,
                    height: cardHeight,
                    background: "transparent",
                    borderColor: theme.node.stroke,
                    "--batch-from-x": `${-x}px`,
                    "--batch-from-y": `${-y}px`,
                    "--batch-from-rotate": `${4 + index * 2}deg`,
                    animation: `canvas-batch-child-in 320ms ${index * 35}ms cubic-bezier(.2,.85,.18,1) both`,
                } as React.CSSProperties
            }
            onMouseDown={(event) => event.stopPropagation()}
            onPointerDown={(event) => event.stopPropagation()}
            onDoubleClick={(event) => {
                if (!image.content || (event.target instanceof Element && event.target.closest("button"))) return;
                event.stopPropagation();
                onView();
            }}
        >
            {image.content ? <img src={source} alt={node.title} draggable={false} className="pointer-events-none h-full w-full select-none object-contain" /> : <ImageSlotStatus image={image} />}
            {image.content ? (
                <div className="pointer-events-none absolute inset-x-2 top-2 flex items-center gap-1 opacity-0 transition-opacity duration-150 group-hover/node:pointer-events-auto group-hover/node:opacity-100">
                    <button type="button" className="flex h-8 min-w-0 flex-1 items-center justify-center gap-1 rounded-lg border px-1.5 text-[10px] font-medium shadow-[0_6px_18px_rgba(15,23,42,.16)] backdrop-blur-md transition hover:scale-[1.02]" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }} title={t("common.download")} onClick={(event) => (event.stopPropagation(), onDownload())}>
                        <Download className="size-3 shrink-0" />
                        <span className="truncate">{t("common.download")}</span>
                    </button>
                    <button type="button" className="flex h-8 min-w-0 flex-1 items-center justify-center gap-1 rounded-lg border px-1.5 text-[10px] font-medium shadow-[0_6px_18px_rgba(15,23,42,.16)] backdrop-blur-md transition hover:scale-[1.02]" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }} title={t("canvas.node.createCopy")} onClick={(event) => (event.stopPropagation(), onDuplicate())}>
                        <Copy className="size-3 shrink-0" />
                        <span className="truncate">{t("canvas.node.createCopy")}</span>
                    </button>
                    <button type="button" className="flex h-8 min-w-0 flex-1 items-center justify-center gap-1 rounded-lg border px-1.5 text-[10px] font-medium shadow-[0_6px_18px_rgba(15,23,42,.16)] backdrop-blur-md transition hover:scale-[1.02]" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.toolbar.activeText }} title={t("canvas.node.setPrimary")} onClick={(event) => (event.stopPropagation(), onSetPrimary())}>
                        <Star className="size-3 shrink-0" style={{ color: selectionBlue }} />
                        <span className="truncate">{t("canvas.node.setPrimary")}</span>
                    </button>
                </div>
            ) : null}
            {image.status === "error" ? <BatchImageFailureActions placement="right" onRetry={onRetry} onDelete={onDelete} /> : null}
        </div>
    );
}

function BatchImageFailureActions({ placement, onRetry, onDelete }: { placement: "left" | "right"; onRetry?: () => void; onDelete: () => void }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    return (
        <div className={`absolute top-3 z-30 flex items-center gap-1.5 ${placement === "left" ? "left-3" : "right-3"}`}>
            <button type="button" disabled={!onRetry} className="flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium shadow-sm transition hover:scale-[1.02]" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.node.text }} onClick={(event) => (event.stopPropagation(), onRetry?.())}>
                <RefreshCw className="size-3.5" />
                {t("canvas.node.retry")}
            </button>
            <button type="button" className="grid size-8 place-items-center rounded-lg border shadow-sm transition hover:scale-[1.02]" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.node.text }} onClick={(event) => (event.stopPropagation(), onDelete())} aria-label={t("common.delete")} title={t("common.delete")}>
                <Trash2 className="size-3.5" />
            </button>
        </div>
    );
}

function ImageSlotStatus({ image }: { image?: CanvasNodeImage }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const { t } = useTranslation();
    const failed = image?.status === "error";
    return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-6 text-center" style={{ background: theme.node.fill, color: failed ? theme.node.text : theme.node.activeStroke }}>
            {failed ? <span className="text-xs leading-5">{image.errorDetails || t("canvas.node.failed")}</span> : <div className="size-10 animate-spin rounded-full border-2" style={{ borderColor: theme.node.stroke, borderTopColor: theme.node.activeStroke }} />}
            {!failed ? <span className="text-[10px] tracking-[0.2em]">{t("canvas.node.generating")}</span> : null}
        </div>
    );
}

function MediaInfoBar({ node, videoInfo }: Pick<CanvasNodeProps, "videoInfo"> & { node: CanvasNodeData }) {
    const isImage = node.type === CanvasNodeType.Image;
    const width = videoInfo?.width || node.metadata?.naturalWidth;
    const height = videoInfo?.height || node.metadata?.naturalHeight;
    const size = formatBytes(node.metadata?.bytes || 0);
    const seconds = Math.floor(videoInfo?.seconds ?? (node.metadata?.durationMs ?? 0) / 1000);
    const timestamp = seconds > 0 ? `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` : "";
    const specs = [width && height ? `${width} × ${height}` : "", isImage ? "" : timestamp].filter(Boolean);
    return <span className="fmv-node-media-info" data-testid="canvas-media-info" title={[...specs, size].filter(Boolean).join(" · ")}>
        {specs.join(" · ") || size}
    </span>;
}

function BatchFrame({ batchCount, batchExpanded, children }: { batchCount: number; batchExpanded: boolean; children: ReactNode }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const isBatchRoot = batchCount > 1;
    return (
        <div className="group/batch relative h-full w-full overflow-visible">
            {isBatchRoot ? (
                <div className="pointer-events-none absolute inset-0 overflow-visible">
                    {Array.from({ length: Math.min(batchCount - 1, 3) }).map((_, index) => (
                        <div
                            key={index}
                            className="absolute rounded-[inherit] border shadow-[0_10px_24px_rgba(68,64,60,.12)] transition-all duration-300 group-hover/batch:translate-x-1"
                            style={{
                                inset: 0,
                                background: `linear-gradient(135deg, ${theme.node.panel}, ${theme.node.fill})`,
                                borderColor: theme.node.stroke,
                                opacity: batchExpanded ? 0 : 1,
                                transform: `translate(${10 + index * 6}px, ${4 + index * 3}px) rotate(${1.5 + index}deg)`,
                                zIndex: -index - 1,
                            }}
                        />
                    ))}
                </div>
            ) : null}
            {children}
        </div>
    );
}
function ResizeHandle({ corner, onMouseDown }: { corner: ResizeCorner; onMouseDown: (event: React.MouseEvent, corner: ResizeCorner) => void }) {
    const positionClass = {
        "top-left": "-left-[14px] -top-[14px] cursor-nwse-resize",
        "top-right": "-right-[14px] -top-[14px] cursor-nesw-resize",
        "bottom-left": "-bottom-[14px] -left-[14px] cursor-nesw-resize",
        "bottom-right": "-bottom-[14px] -right-[14px] cursor-nwse-resize",
    }[corner];

    return <div className={`absolute z-50 size-7 ${positionClass}`} data-canvas-node-control onMouseDown={(event) => onMouseDown(event, corner)} />;
}

function ConnectionHandleDot({ nodeId, side, active, onMouseDown }: { nodeId: string; side: "left" | "right"; active: boolean; onMouseDown: (event: React.MouseEvent) => void }) {
    return (
        <div className="fmv-node-port" data-side={side} data-active={active}>
            <button
                type="button"
                data-testid={side === 'left' ? 'canvas-connection-target' : 'canvas-connection-source'}
                data-node-id={nodeId}
                aria-label="拖拽连线或点击选择节点类型"
                className="fmv-node-port-button"
                onMouseDown={onMouseDown}
                onClick={onMouseDown}
            >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 3.25v9.5M3.25 8h9.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
            </button>
        </div>
    );
}
