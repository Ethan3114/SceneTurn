import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from 'react';

type ImageSize = { width: number; height: number };
const minZoom = 1, maxZoom = 4, zoomStep = 1.2;

export function useImageEditorViewport(image: ImageSize | null, open: boolean) {
  const viewportNodeRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const panRef = useRef<{ pointerId: number; x: number; y: number; scrollLeft: number; scrollTop: number } | null>(null);
  const zoomAnchorRef = useRef<{ zoom: number; ratioX: number; ratioY: number; viewportX: number; viewportY: number } | null>(null);
  const [viewportElement, setViewportElement] = useState<HTMLDivElement | null>(null);
  const [viewportSize, setViewportSize] = useState<ImageSize>({ width: 0, height: 0 });
  const [zoom, setZoom] = useState(minZoom);
  const [isPanning, setIsPanning] = useState(false);
  const [spacePressed, setSpacePressed] = useState(false);
  const spacePressedRef = useRef(false);
  const viewportRef = useCallback((node: HTMLDivElement | null) => { viewportNodeRef.current = node; setViewportElement(node); }, []);

  useEffect(() => { if (open) { zoomAnchorRef.current = null; setZoom(minZoom); } }, [open, image?.width, image?.height]);
  useEffect(() => {
    if (!open) return;
    const release = () => { spacePressedRef.current = false; setSpacePressed(false); };
    const down = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || event.repeat) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("input,textarea,[contenteditable='true']")) return;
      event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
      if (document.activeElement instanceof HTMLElement && document.activeElement.matches("button,a,[role='button']")) document.activeElement.blur();
      spacePressedRef.current = true; setSpacePressed(true);
    };
    const up = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || !spacePressedRef.current) return;
      event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation(); release();
    };
    window.addEventListener('keydown', down, true); window.addEventListener('keyup', up, true); window.addEventListener('blur', release);
    return () => { window.removeEventListener('keydown', down, true); window.removeEventListener('keyup', up, true); window.removeEventListener('blur', release); spacePressedRef.current = false; };
  }, [open]);
  useEffect(() => {
    if (!open || !viewportElement) return;
    const measure = () => setViewportSize({ width: viewportElement.clientWidth, height: viewportElement.clientHeight });
    measure(); const observer = new ResizeObserver(measure); observer.observe(viewportElement); return () => observer.disconnect();
  }, [open, viewportElement]);

  const ratio = image && viewportSize.width && viewportSize.height ? Math.min((viewportSize.width - 32) / image.width, (viewportSize.height - 32) / image.height, 1) : 0;
  const baseSize = { width: Math.max(0, Math.floor((image?.width || 0) * ratio)), height: Math.max(0, Math.floor((image?.height || 0) * ratio)) };
  const stageSize = { width: baseSize.width * zoom, height: baseSize.height * zoom };
  const contentSize = { width: Math.max(viewportSize.width, stageSize.width), height: Math.max(viewportSize.height, stageSize.height) };
  const stageOffset = { left: Math.max(0, (contentSize.width - stageSize.width) / 2), top: Math.max(0, (contentSize.height - stageSize.height) / 2) };

  useLayoutEffect(() => {
    const viewport = viewportNodeRef.current, anchor = zoomAnchorRef.current;
    if (!viewport || !anchor || Math.abs(anchor.zoom - zoom) > .001) return;
    const width = baseSize.width * zoom, height = baseSize.height * zoom;
    viewport.scrollLeft = Math.max(0, (Math.max(viewport.clientWidth, width) - width) / 2) + anchor.ratioX * width - anchor.viewportX;
    viewport.scrollTop = Math.max(0, (Math.max(viewport.clientHeight, height) - height) / 2) + anchor.ratioY * height - anchor.viewportY;
    zoomAnchorRef.current = null;
  }, [baseSize.width, baseSize.height, zoom]);
  const setZoomAround = useCallback((next: number, clientX?: number, clientY?: number) => {
    const viewport = viewportNodeRef.current, stage = stageRef.current;
    if (!viewport || !stage || !baseSize.width || !baseSize.height) return;
    const bounded = Math.min(maxZoom, Math.max(minZoom, next));
    if (Math.abs(bounded - zoom) < .001) return;
    const box = viewport.getBoundingClientRect(), stageBox = stage.getBoundingClientRect();
    const x = clientX ?? box.left + box.width / 2, y = clientY ?? box.top + box.height / 2;
    zoomAnchorRef.current = { zoom: bounded, ratioX: Math.min(1, Math.max(0, (x - stageBox.left) / Math.max(1, stageBox.width))), ratioY: Math.min(1, Math.max(0, (y - stageBox.top) / Math.max(1, stageBox.height))), viewportX: x - box.left, viewportY: y - box.top };
    setZoom(bounded);
  }, [baseSize.width, baseSize.height, zoom]);
  useEffect(() => {
    if (!open || !viewportElement) return;
    const wheel = (event: WheelEvent) => { event.preventDefault(); event.stopPropagation(); setZoomAround(event.deltaY < 0 ? zoom * zoomStep : zoom / zoomStep, event.clientX, event.clientY); };
    viewportElement.addEventListener('wheel', wheel, { passive: false }); return () => viewportElement.removeEventListener('wheel', wheel);
  }, [open, viewportElement, zoom, setZoomAround]);
  const start = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 1 && !(event.button === 0 && spacePressedRef.current)) return;
    event.preventDefault(); event.stopPropagation();
    panRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, scrollLeft: event.currentTarget.scrollLeft, scrollTop: event.currentTarget.scrollTop };
    event.currentTarget.setPointerCapture(event.pointerId); setIsPanning(true);
  }, []);
  const move = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current; if (!pan || pan.pointerId !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    event.currentTarget.scrollLeft = pan.scrollLeft - event.clientX + pan.x; event.currentTarget.scrollTop = pan.scrollTop - event.clientY + pan.y;
  }, []);
  const stop = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (panRef.current?.pointerId !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    panRef.current = null; setIsPanning(false);
  }, []);
  return {
    viewportRef, stageRef, zoom, isPanning, spacePressed,
    scrollClassName: zoom > minZoom + .001 ? 'overflow-scroll' : 'overflow-hidden',
    panHandlers: { onPointerDownCapture: start, onPointerMoveCapture: move, onPointerUpCapture: stop, onPointerCancelCapture: stop, onAuxClick: (event: ReactMouseEvent) => { if (event.button === 1) { event.preventDefault(); event.stopPropagation(); } } },
    canZoomIn: zoom < maxZoom, canZoomOut: zoom > minZoom,
    zoomIn: () => setZoomAround(zoom * zoomStep), zoomOut: () => setZoomAround(zoom / zoomStep), resetZoom: () => setZoomAround(minZoom),
    contentStyle: contentSize satisfies CSSProperties,
    stageStyle: { ...stageOffset, ...stageSize } satisfies CSSProperties,
    mediaStyle: { width: baseSize.width, height: baseSize.height, transform: `scale(${zoom})`, transformOrigin: 'top left' } satisfies CSSProperties,
  };
}
