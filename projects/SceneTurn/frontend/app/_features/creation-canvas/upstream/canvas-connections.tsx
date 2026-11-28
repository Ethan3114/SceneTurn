import { useState, type MouseEvent as ReactMouseEvent } from "react";

import { getBezierPath, Position as PortPosition } from "@xyflow/react";

import type { CanvasConnection, CanvasNodeData, ConnectionHandle, Position } from "./canvas";

function portAnchor(node: CanvasNodeData, side: ConnectionHandle["handleType"]): Position {
    return {
        x: node.position.x + (side === "source" ? node.width : 0),
        y: node.position.y + node.height / 2,
    };
}

function connectionCurve(source: Position, target: Position) {
    const [path] = getBezierPath({
        sourceX: source.x, sourceY: source.y, sourcePosition: PortPosition.Right,
        targetX: target.x, targetY: target.y, targetPosition: PortPosition.Left,
    });
    return path;
}

export function ConnectionPath({
    connection,
    from,
    to,
    active,
    onSelect,
    onContextMenu,
}: {
    connection: CanvasConnection;
    from: CanvasNodeData;
    to: CanvasNodeData;
    active: boolean;
    onSelect: () => void;
    onContextMenu?: (event: ReactMouseEvent<SVGPathElement>) => void;
}) {
    const [hovered, setHovered] = useState(false);
    const path = connectionCurve(portAnchor(from, "source"), portAnchor(to, "target"));

    return (
        <g className="fmv-canvas-connection" data-selected={active} data-hovered={hovered}>
            <path
                data-connection-id={connection.id}
                d={path}
                stroke="transparent"
                strokeWidth="20"
                fill="none"
                style={{ cursor: "pointer", pointerEvents: "stroke" }}
                onPointerEnter={() => setHovered(true)}
                onPointerLeave={() => setHovered(false)}
                onClick={(event) => {
                    event.stopPropagation();
                    onSelect();
                }}
                onContextMenu={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    onContextMenu?.(event);
                }}
            />
            <path d={path} className="fmv-canvas-connection-stroke" fill="none" stroke="#86909c" strokeWidth="2" pointerEvents="none" />
            {(hovered || active) && <g className="fmv-canvas-connection-current" fill="none" stroke="#64b4ff" pointerEvents="none" aria-hidden="true">
                <path d={path} strokeWidth="12" strokeLinecap="round" strokeDasharray="36 64" strokeOpacity="0.06" />
                <path d={path} strokeWidth="7" strokeLinecap="round" strokeDasharray="36 64" strokeOpacity="0.12" />
                <path d={path} strokeWidth="4" strokeDasharray="36 64" strokeOpacity="0.15" />
                {[0.176471, 0.214286, 0.272727, 0.375, 0.6].map((opacity, index) => {
                    const offset = (index + 1) * 6;
                    return <path key={offset} d={path} strokeWidth="4" strokeDasharray={`0 ${offset} ${36 - offset} 64`} strokeOpacity={opacity} />;
                })}
            </g>}
        </g>
    );
}

export function ActiveConnectionPath({ node, handle, mouseWorld, target }: { node?: CanvasNodeData; handle: ConnectionHandle; mouseWorld: Position; target?: CanvasNodeData }) {
    if (!node) return null;

    const nearestSource = target ? portAnchor(target, "source") : mouseWorld;
    const nearestTarget = target ? portAnchor(target, "target") : mouseWorld;
    const source = handle.handleType === "source" ? portAnchor(node, "source") : nearestSource;
    const destination = handle.handleType === "target" ? portAnchor(node, "target") : nearestTarget;

    return <path d={connectionCurve(source, destination)} className="fmv-canvas-connection-draft" stroke="#86909c" strokeWidth="2" fill="none" />;
}
