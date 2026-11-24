import React, { useEffect, useRef, useState } from "react";

import { canvasThemes, type CanvasBackgroundMode } from "./canvas-theme";
import { useThemeStore } from "./use-theme-store";
import type { Position, ViewportTransform } from "./canvas";

type InfiniteCanvasProps = {
    containerRef: React.RefObject<HTMLDivElement | null>;
    viewport: ViewportTransform;
    backgroundMode?: CanvasBackgroundMode;
    onViewportChange: (viewport: ViewportTransform) => void;
    onCanvasDeselect?: () => void;
    onNodeSelect: (nodeId: string, additive: boolean) => void;
    onNodeDragStart: (nodeId: string) => ((delta: Position) => void) | undefined;
    onDragChange: (dragging: boolean) => void;
    onGestureStart: () => void;
    onSelectArea?: (start: Position, end: Position, additive: boolean) => void;
    onCanvasDoubleClick?: (event: React.MouseEvent<HTMLDivElement>) => void;
    onContextMenu?: (event: React.MouseEvent) => void;
    onDrop?: (event: React.DragEvent<HTMLDivElement>) => void;
    children: React.ReactNode;
};

const HOLD_TO_MOVE_MS = 280;
const MOVEMENT_THRESHOLD = 4;

type CanvasGesture = {
    pointerId: number;
    start: Position;
    initialViewport: ViewportTransform;
    nodeId?: string;
    startedAt: number;
    additive: boolean;
    forcePan: boolean;
    mode: "pending" | "pan" | "node" | "area";
    moveNode?: (delta: Position) => void;
};

export function InfiniteCanvas({ containerRef, viewport, backgroundMode = "dots", onViewportChange, onCanvasDeselect, onNodeSelect, onNodeDragStart, onDragChange, onGestureStart, onSelectArea, onCanvasDoubleClick, onContextMenu, onDrop, children }: InfiniteCanvasProps) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    const gesture = useRef<CanvasGesture | null>(null);
    const isSpacePressed = useRef(false);
    const [dragMode, setDragMode] = useState<CanvasGesture["mode"]>("pending");
    const [selectionBox, setSelectionBox] = useState<{ left: number; top: number; width: number; height: number } | null>(null);

    useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.code !== "Space" || !containerRef.current?.contains(document.activeElement)) return;
            const target = event.target instanceof Element ? event.target : null;
            if (target?.closest("input,textarea,select,button,a,[contenteditable='true']")) return;
            event.preventDefault();
            isSpacePressed.current = true;
        };
        const handleKeyUp = (event: KeyboardEvent) => {
            if (event.code === "Space") isSpacePressed.current = false;
        };
        const handleBlur = () => { isSpacePressed.current = false; };
        window.addEventListener("keydown", handleKeyDown);
        window.addEventListener("keyup", handleKeyUp);
        window.addEventListener("blur", handleBlur);
        return () => {
            window.removeEventListener("keydown", handleKeyDown);
            window.removeEventListener("keyup", handleKeyUp);
            window.removeEventListener("blur", handleBlur);
        };
    }, [containerRef]);

    const handleWheel = (event: React.WheelEvent<HTMLDivElement>) => {
        const target = event.target instanceof Element ? event.target : null;
        if (gesture.current || target?.closest("[data-canvas-no-zoom],.ant-modal,.ant-popover,.ant-dropdown,.ant-select-dropdown,.ant-picker-dropdown")) return;

        const delta = -event.deltaY;
        const factor = Math.pow(1.1, delta / 100);
        const newScale = Math.min(Math.max(viewport.k * factor, 0.05), 5);
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) return;

        const mouseX = event.clientX - rect.left;
        const mouseY = event.clientY - rect.top;
        const worldX = (mouseX - viewport.x) / viewport.k;
        const worldY = (mouseY - viewport.y) / viewport.k;

        onViewportChange({
            x: mouseX - worldX * newScale,
            y: mouseY - worldY * newScale,
            k: newScale,
        });
    };

    const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !event.currentTarget.contains(target) || gesture.current || (event.button !== 0 && event.button !== 1)) return;
        if (target.closest("[data-connection-create-menu],[data-connection-id],[data-node-panel],[data-canvas-node-control]")) return;
        const nodeId = target.closest<HTMLElement>("[data-canvas-drag-node]")?.dataset.canvasDragNode;
        if (!nodeId && target.closest("[data-node-id],[data-canvas-no-zoom]")) return;
        if (target.closest("button:not([data-canvas-node-title]),input,textarea,select,a,summary,video,audio,[contenteditable='true']")) return;
        onGestureStart();
        if (!nodeId) event.currentTarget.focus({ preventScroll: true });
        if (event.button === 1) event.preventDefault();
        gesture.current = {
            pointerId: event.pointerId,
            start: { x: event.clientX, y: event.clientY },
            initialViewport: viewport,
            startedAt: event.timeStamp,
            nodeId,
            additive: event.shiftKey || event.metaKey || event.ctrlKey,
            forcePan: event.button === 1 || isSpacePressed.current,
            mode: "pending",
        };
    };

    const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
        const current = gesture.current;
        if (!current || current.pointerId !== event.pointerId) return;
        const delta = { x: event.clientX - current.start.x, y: event.clientY - current.start.y };
        if (current.mode === "pending") {
            if (Math.max(Math.abs(delta.x), Math.abs(delta.y)) < MOVEMENT_THRESHOLD) return;
            const held = event.timeStamp - current.startedAt >= HOLD_TO_MOVE_MS;
            if (!current.forcePan && current.nodeId && held) current.moveNode = onNodeDragStart(current.nodeId);
            current.mode = current.moveNode ? "node" : !current.forcePan && !current.nodeId && current.additive && onSelectArea ? "area" : "pan";
            event.currentTarget.setPointerCapture(event.pointerId);
            setDragMode(current.mode);
            if (current.mode === "node") onDragChange(true);
        }
        if (current.mode === "node") {
            const scale = current.initialViewport.k;
            current.moveNode?.({ x: delta.x / scale, y: delta.y / scale });
        } else if (current.mode === "pan") {
            onViewportChange({ x: current.initialViewport.x + delta.x, y: current.initialViewport.y + delta.y, k: current.initialViewport.k });
        } else {
            const rect = event.currentTarget.getBoundingClientRect();
            setSelectionBox({ left: Math.min(current.start.x, event.clientX) - rect.left, top: Math.min(current.start.y, event.clientY) - rect.top, width: Math.abs(delta.x), height: Math.abs(delta.y) });
        }
    };

    const finishPointer = (event: React.PointerEvent<HTMLDivElement>, completed: boolean) => {
        const current = gesture.current;
        if (!current || current.pointerId !== event.pointerId) return;
        gesture.current = null;
        setDragMode("pending");
        if (current.mode === "node") onDragChange(false);
        if (current.mode === "area") {
            setSelectionBox(null);
            if (completed) onSelectArea?.(current.start, { x: event.clientX, y: event.clientY }, current.additive);
        }
        if (!completed || current.mode !== "pending" || event.button !== 0 || current.forcePan) return;
        if (current.nodeId) onNodeSelect(current.nodeId, current.additive);
        else if (!current.additive) onCanvasDeselect?.();
    };

    const handleDoubleClick = (event: React.MouseEvent<HTMLDivElement>) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !event.currentTarget.contains(target)) return;
        if (target?.closest("[data-canvas-no-zoom],[data-node-id],[data-connection-id]")) return;
        onCanvasDoubleClick?.(event);
    };

    useEffect(() => {
        const container = containerRef.current;
        if (!container) return;

        // 保留弹层滚动，阻止画布滚轮带动页面。
        const preventWheelScroll = (event: WheelEvent) => {
            const target = event.target instanceof Element ? event.target : null;
            if (target?.closest("[data-canvas-no-zoom],.ant-modal,.ant-popover,.ant-dropdown,.ant-select-dropdown,.ant-picker-dropdown")) return;
            event.preventDefault();
        };
        container.addEventListener("wheel", preventWheelScroll, { passive: false });
        return () => container.removeEventListener("wheel", preventWheelScroll);
    }, [containerRef]);

    const cursor = dragMode === "pan" ? "grabbing" : dragMode === "node" ? "move" : dragMode === "area" ? "crosshair" : "grab";

    return (
        <div
            ref={containerRef}
            tabIndex={-1}
            data-testid="canvas-surface"
            className="relative h-full w-full select-none overflow-clip"
            style={{ background: backgroundMode === 'dots' ? '#1a1a1a' : theme.canvas.background, cursor, touchAction: 'none' }}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={event => finishPointer(event, true)}
            onPointerCancel={event => finishPointer(event, false)}
            onLostPointerCapture={event => finishPointer(event, false)}
            onPointerLeave={event => { if (gesture.current?.mode === "pending") finishPointer(event, false); }}
            onDoubleClick={handleDoubleClick}
            onWheel={handleWheel}
            onContextMenu={onContextMenu}
            onDragOver={(event) => event.preventDefault()}
            onDrop={onDrop}
        >
            <CanvasGrid viewport={viewport} mode={backgroundMode} />
            {selectionBox && <div aria-hidden="true" className="fmv-canvas-selection-box" style={selectionBox} />}
            <div
                className="absolute origin-top-left"
                style={{
                    transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.k})`,
                }}
            >
                {children}
            </div>
        </div>
    );
}

function CanvasGrid({ viewport, mode }: { viewport: ViewportTransform; mode: CanvasBackgroundMode }) {
    const theme = canvasThemes[useThemeStore((state) => state.theme)];
    if (mode === "blank") return null;

    const gridSize = (mode === 'dots' ? 16 : 48) * viewport.k;
    const x = viewport.x % gridSize;
    const y = viewport.y % gridSize;
    const dotSize = 0.5 * viewport.k;
    const backgroundImage =
        mode === "dots" ? `radial-gradient(circle, #474747 ${dotSize}px, transparent ${dotSize + 0.2}px)` : `linear-gradient(${theme.canvas.line} 1px, transparent 1px), linear-gradient(90deg, ${theme.canvas.line} 1px, transparent 1px)`;

    return (
        <div
            className="pointer-events-none absolute inset-0"
            style={{
                backgroundImage,
                backgroundSize: `${gridSize}px ${gridSize}px`,
                backgroundPosition: `${x}px ${y}px`,
            }}
        />
    );
}
