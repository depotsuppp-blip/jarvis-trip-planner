"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import {
  displayName,
  groupInfo,
  groupOf,
  isMe,
  RELATION_DASH,
  relationLabel,
  type MemoryLink,
  type MemoryNode,
  type TypeGroupId,
} from "@/lib/jarvis/memory";

/**
 * The memory graph itself: a force-directed canvas (react-force-graph-2d) of the person's
 * entries and relations. Loaded only in the browser (MemoryView imports it through
 * next/dynamic with ssr: false - the library touches `window` as it loads).
 *
 * Drawn by hand on the canvas, so it speaks the hub's Editorial dialect:
 *   - a node is a dot in its type group's colour (lib/jarvis/memory.ts TYPE_GROUPS) with a 2px
 *     Paper ring, sized by how many relations it has; the person's own entry ("me") is larger
 *     with an Ink ring. The selected node gets the rose ring - Deep Signal Rose means "this one".
 *   - its name under it, 12px on screen at any zoom, in Ink Strong on a Paper halo, wherever it
 *     does not cover another name (zooming in makes room); the names that matter right now -
 *     selected, hovered, search matches, "me" - are always drawn.
 *   - a relation is a Perforation line with an arrow at its target, thicker the more often it
 *     was told; its words ("sister of") sit on it, along it, once the view is close enough or
 *     when it touches the selected or hovered entry. A fact that ended keeps its line, drawn
 *     as history: dashed when completed, dotted when archived (RELATION_DASH).
 *   - a search dims everything that does not match; the type filter hides the other groups.
 *
 * The library's own hover tooltip is switched off: it writes its text as HTML, and names come
 * from what people said. Hovering shows this component's own (text-only) tooltip instead.
 */

export type GraphNode = MemoryNode & { x?: number; y?: number };
export type GraphLink = Omit<MemoryLink, "source" | "target"> & { source: string | GraphNode; target: string | GraphNode };
type FGNode = NodeObject<GraphNode>;
type FGLink = LinkObject<GraphNode, GraphLink>;

export interface GraphCanvasApi {
  /** Zooms so every visible entry is in view. */
  fit: (durationMs?: number) => void;
  /**
   * Glides to one entry and zooms in on it, centred in what is left of the canvas above
   * `bottomInset` px (the phone's bottom sheet covers the rest).
   */
  focus: (id: string, bottomInset?: number) => void;
}

export interface MemoryGraphCanvasProps {
  /** Same object until entries or relations are added or removed (a new one re-runs the layout). */
  graph: { nodes: GraphNode[]; links: GraphLink[] };
  /** Bumped when an entry's words change in place, so the canvas paints them. */
  revision: number;
  selectedId: string | null;
  /** The search's matches, or null when there is no search. */
  matchIds: ReadonlySet<string> | null;
  /** "all", or the one type group to show. */
  group: TypeGroupId | "all";
  onSelect: (id: string | null) => void;
  onReady: (api: GraphCanvasApi) => void;
}

const PAPER = "#ffffff";
const INK = "#0f172b";
const INK_STRONG = "#314158";
const INK_BODY = "#45556c";
const PERFORATION = "#cad5e2";
const ROSE = "#ec003f";
const FONT = "Geist, Arial, sans-serif";
// Below this zoom (a big graph seen whole) the canvas only names what matters right now.
const MIN_LABEL_ZOOM = 0.35;
// From this zoom every relation shows its words (where they fit without overlapping another's).
const LINK_LABEL_ZOOM = 1.6;
// How hard the middle pulls (d3's forceX/forceY default is 0.1).
const GRAVITY = 0.05;
// Room around the graph when it is fitted: the legend sits over the bottom-left corner.
const FIT_PADDING = 72;

export const endId = (end: string | GraphNode | undefined): string =>
  typeof end === "object" && end !== null ? end.id : String(end ?? "");

function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

function canvasFont(ctx: CanvasRenderingContext2D): string {
  // The CSS variable is not resolved inside a canvas font string: read Geist's real family once.
  const family = getComputedStyle(ctx.canvas).getPropertyValue("--font-geist-sans").trim();
  return family ? `${family}, Arial, sans-serif` : FONT;
}

export default function MemoryGraphCanvas({
  graph,
  revision,
  selectedId,
  matchIds,
  group,
  onSelect,
  onReady,
}: MemoryGraphCanvasProps) {
  const box = useRef<HTMLDivElement | null>(null);
  const tooltip = useRef<HTMLDivElement | null>(null);
  const fg = useRef<ForceGraphMethods<FGNode, FGLink> | undefined>(undefined);
  const fitted = useRef(false);
  const family = useRef<string | null>(null);
  // The relation labels drawn this frame (screen-independent graph units), so two never overlap.
  const labelBoxes = useRef<[number, number, number, number][]>([]);
  // ... and the entries' names, likewise.
  const nameBoxes = useRef<[number, number, number, number][]>([]);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [hovered, setHovered] = useState<GraphNode | null>(null);
  const mounted = size !== null;

  // Spacing: stronger repulsion and longer links than the library's defaults, so dots and their
  // names do not sit on each other, and a gentle pull to the middle, so an entry with no
  // relations stays near the rest instead of drifting to the edge of the canvas.
  useEffect(() => {
    const api = fg.current;
    if (!api) return;
    api.d3Force("charge")?.strength(-150).distanceMax(360);
    api.d3Force("link")?.distance(58);
    let nodes: FGNode[] = [];
    const gravity = Object.assign(
      (alpha: number) => {
        for (const node of nodes) {
          node.vx = (node.vx ?? 0) - (node.x ?? 0) * GRAVITY * alpha;
          node.vy = (node.vy ?? 0) - (node.y ?? 0) * GRAVITY * alpha;
        }
      },
      { initialize: (all: FGNode[]) => void (nodes = all) },
    );
    api.d3Force("gravity", gravity);
    api.d3ReheatSimulation();
  }, [mounted]);

  // The canvas fills its box; the library needs the size in numbers.
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      setSize({ width: Math.max(1, Math.round(rect.width)), height: Math.max(1, Math.round(rect.height)) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // How many relations each entry has, and the bend that keeps two relations between the same pair apart.
  const { degree, curvature } = useMemo(() => {
    const degree = new Map<string, number>();
    const pairs = new Map<string, GraphLink[]>();
    for (const link of graph.links) {
      const s = endId(link.source);
      const t = endId(link.target);
      degree.set(s, (degree.get(s) ?? 0) + 1);
      degree.set(t, (degree.get(t) ?? 0) + 1);
      const pair = s < t ? `${s}|${t}` : `${t}|${s}`;
      pairs.set(pair, [...(pairs.get(pair) ?? []), link]);
    }
    const curvature = new Map<string, number>();
    for (const links of pairs.values()) {
      if (links.length < 2) continue;
      links.forEach((link, index) => {
        // 0.25, -0.25, 0.5, -0.5... measured against one direction of the pair.
        const step = Math.ceil((index + 1) / 2) * 0.25 * (index % 2 ? -1 : 1);
        const forward = endId(link.source) < endId(link.target);
        curvature.set(link.id, forward ? step : -step);
      });
    }
    return { degree, curvature };
  }, [graph]);

  const radiusOf = useCallback(
    (node: GraphNode) => 5 + Math.min(6, Math.sqrt(degree.get(node.id) ?? 0) * 1.6) + (isMe(node) ? 2 : 0),
    [degree],
  );

  const visible = useCallback((node: GraphNode) => group === "all" || groupOf(node.type) === group, [group]);
  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);
  const linkVisible = useCallback(
    (link: GraphLink) => {
      const s = nodeById.get(endId(link.source));
      const t = nodeById.get(endId(link.target));
      return !!s && !!t && visible(s) && visible(t);
    },
    [nodeById, visible],
  );

  const touches = useCallback(
    (link: GraphLink) => {
      const s = endId(link.source);
      const t = endId(link.target);
      return (
        (selectedId !== null && (s === selectedId || t === selectedId)) ||
        (hovered !== null && (s === hovered.id || t === hovered.id))
      );
    },
    [selectedId, hovered],
  );

  const dimmed = useCallback((id: string) => matchIds !== null && !matchIds.has(id), [matchIds]);

  // ---- the canvas API the page drives --------------------------------------------------------

  useEffect(() => {
    onReady({
      fit: (durationMs = 400) => fg.current?.zoomToFit(durationMs, FIT_PADDING, (node) => visible(node as GraphNode)),
      focus: (id, bottomInset = 0) => {
        const node = nodeById.get(id);
        const graphApi = fg.current;
        if (!node || !graphApi || node.x === undefined || node.y === undefined) return;
        const zoom = Math.max(graphApi.zoom(), 2.2);
        // The view's centre moves down by half the covered height, so the entry sits in the middle of the rest.
        graphApi.centerAt(node.x, node.y + bottomInset / 2 / zoom, 500);
        graphApi.zoom(zoom, 500);
      },
    });
  }, [onReady, nodeById, visible]);

  // A new filter frames what it leaves.
  useEffect(() => {
    if (fitted.current) fg.current?.zoomToFit(400, FIT_PADDING, (node) => visible(node as GraphNode));
  }, [visible]);

  // ---- painting ----------------------------------------------------------------------------------

  const paintNode = useCallback(
    (raw: FGNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const node = raw as GraphNode;
      if (node.x === undefined || node.y === undefined) return;
      const r = radiusOf(node);
      const faded = dimmed(node.id);
      const selected = node.id === selectedId;
      const me = isMe(node);
      ctx.globalAlpha = faded ? 0.18 : 1;

      // The fill, with a Paper ring so overlapping dots stay apart.
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
      ctx.fillStyle = groupInfo(groupOf(node.type)).color;
      ctx.fill();
      ctx.lineWidth = 2 / scale;
      ctx.strokeStyle = PAPER;
      ctx.stroke();
      if (me || selected) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 2.5 / scale, 0, 2 * Math.PI);
        ctx.lineWidth = 2 / scale;
        ctx.strokeStyle = selected ? ROSE : INK;
        ctx.stroke();
      }
      if (hovered?.id === node.id && !selected) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 2.5 / scale, 0, 2 * Math.PI);
        ctx.lineWidth = 1 / scale;
        ctx.strokeStyle = INK_BODY;
        ctx.stroke();
      }

      // The name, 12px on screen whatever the zoom - where it has room: a name that would cover
      // one already drawn this frame is left out (zoom in and it comes back), except the names
      // that matter right now, which are always drawn.
      const important = selected || me || hovered?.id === node.id || (matchIds !== null && matchIds.has(node.id));
      if (scale >= MIN_LABEL_ZOOM || important) {
        family.current ??= canvasFont(ctx);
        const size = 12 / scale;
        ctx.font = `${selected || me ? 600 : 500} ${size}px ${family.current}`;
        const text = clip(displayName(node), 28);
        const y = node.y + r + 3 / scale;
        const halfW = ctx.measureText(text).width / 2 + 2 / scale;
        const box: [number, number, number, number] = [node.x, y + size * 0.6, halfW, size * 0.7];
        const boxes = nameBoxes.current;
        if (!important && boxes.some(([bx, by, bw, bh]) => Math.abs(bx - box[0]) < bw + box[2] && Math.abs(by - box[1]) < bh + box[3])) {
          ctx.globalAlpha = 1;
          return;
        }
        boxes.push(box);
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.lineJoin = "round";
        ctx.lineWidth = 3 / scale;
        ctx.strokeStyle = PAPER;
        ctx.strokeText(text, node.x, y);
        ctx.fillStyle = selected ? INK : INK_STRONG;
        ctx.fillText(text, node.x, y);
      }
      ctx.globalAlpha = 1;
    },
    // revision: an entry's words changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [radiusOf, dimmed, selectedId, hovered, matchIds, revision],
  );

  const paintPointerArea = useCallback(
    (raw: FGNode, color: string, ctx: CanvasRenderingContext2D, scale: number) => {
      const node = raw as GraphNode;
      if (node.x === undefined || node.y === undefined) return;
      // Bigger than the dot: easy to hit with a finger.
      ctx.beginPath();
      ctx.arc(node.x, node.y, radiusOf(node) + 6 / scale, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [radiusOf],
  );

  const linkColor = useCallback(
    (raw: FGLink) => {
      const link = raw as GraphLink;
      if (touches(link)) return INK_BODY;
      if (matchIds !== null && (dimmed(endId(link.source)) || dimmed(endId(link.target)))) return "rgba(202, 213, 226, 0.35)";
      return PERFORATION;
    },
    [touches, dimmed, matchIds],
  );

  const linkDash = useCallback(
    (raw: FGLink) => RELATION_DASH[(raw as GraphLink).status] ?? null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [revision],
  );

  const linkWidth = useCallback(
    (raw: FGLink) => {
      const link = raw as GraphLink;
      const base = 1 + Math.min(2, Math.log2(Math.max(1, link.weight)));
      return touches(link) ? base + 0.5 : base;
    },
    [touches],
  );

  const paintLinkLabel = useCallback(
    (raw: FGLink, ctx: CanvasRenderingContext2D, scale: number) => {
      const link = raw as GraphLink;
      const s = typeof link.source === "object" ? link.source : null;
      const t = typeof link.target === "object" ? link.target : null;
      if (!s || !t || s.x === undefined || s.y === undefined || t.x === undefined || t.y === undefined) return;
      const near = touches(link);
      if (!near && scale < LINK_LABEL_ZOOM) return;
      if (matchIds !== null && !near && (dimmed(s.id) || dimmed(t.id))) return;

      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const length = Math.hypot(dx, dy);
      family.current ??= canvasFont(ctx);
      const size = 10 / scale;
      ctx.font = `500 ${size}px ${family.current}`;
      const text = clip(relationLabel(link.relation_type), 24);
      const textWidth = ctx.measureText(text).width;
      // No room between the two dots: leave it to the inspector.
      if (length - radiusOf(s) - radiusOf(t) < textWidth + 4 / scale) return;

      // The middle of the (possibly bent) line: force-graph bends it by curvature x length.
      const bend = curvature.get(link.id) ?? 0;
      const angle = Math.atan2(dy, dx);
      const offset = (length * bend) / 2;
      const x = (s.x + t.x) / 2 + offset * Math.cos(angle - Math.PI / 2);
      const y = (s.y + t.y) / 2 + offset * Math.sin(angle - Math.PI / 2);

      // Where the turned label lands, as a box; one that would cover another label is left out,
      // except on the selected or hovered entry's relations, which always say what they are.
      const w = textWidth + 4 / scale;
      const h = size * 1.4;
      const cos = Math.abs(Math.cos(angle));
      const sin = Math.abs(Math.sin(angle));
      const halfW = (cos * w + sin * h) / 2;
      const halfH = (sin * w + cos * h) / 2;
      const boxes = labelBoxes.current;
      if (!near && boxes.some(([bx, by, bw, bh]) => Math.abs(bx - x) < bw + halfW && Math.abs(by - y) < bh + halfH)) return;
      boxes.push([x, y, halfW, halfH]);

      ctx.save();
      ctx.translate(x, y);
      // Always readable left to right.
      ctx.rotate(angle > Math.PI / 2 || angle < -Math.PI / 2 ? angle + Math.PI : angle);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.lineJoin = "round";
      ctx.lineWidth = 3 / scale;
      ctx.strokeStyle = PAPER;
      ctx.strokeText(text, 0, 0);
      ctx.fillStyle = near ? INK_STRONG : INK_BODY;
      ctx.fillText(text, 0, 0);
      ctx.restore();
    },
    [touches, matchIds, dimmed, radiusOf, curvature],
  );

  // ---- the hover tooltip (text only, moved without re-rendering) -----------------------------------

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const tip = tooltip.current;
    const element = box.current;
    if (!tip || !element) return;
    const rect = element.getBoundingClientRect();
    const x = Math.min(event.clientX - rect.left + 14, rect.width - tip.offsetWidth - 8);
    const y = Math.min(event.clientY - rect.top + 16, rect.height - tip.offsetHeight - 8);
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }, []);

  const hoveredCount = hovered ? (degree.get(hovered.id) ?? 0) : 0;

  return (
    <div ref={box} className="absolute inset-0" onPointerMove={onPointerMove} onPointerLeave={() => setHovered(null)}>
      {size && (
        <ForceGraph2D<GraphNode, GraphLink>
          ref={fg}
          graphData={graph}
          width={size.width}
          height={size.height}
          backgroundColor={PAPER}
          nodeId="id"
          nodeRelSize={1}
          nodeVal={(node) => radiusOf(node as GraphNode) ** 2}
          nodeLabel={() => ""}
          linkLabel={() => ""}
          nodeVisibility={(node) => visible(node as GraphNode)}
          linkVisibility={(link) => linkVisible(link as GraphLink)}
          nodeCanvasObject={paintNode}
          nodePointerAreaPaint={paintPointerArea}
          linkColor={linkColor}
          linkLineDash={linkDash}
          linkWidth={linkWidth}
          linkCurvature={(link) => curvature.get((link as GraphLink).id) ?? 0}
          linkDirectionalArrowLength={5}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={linkColor}
          linkCanvasObjectMode={() => "after"}
          linkCanvasObject={paintLinkLabel}
          onRenderFramePre={() => {
            labelBoxes.current = [];
            nameBoxes.current = [];
          }}
          cooldownTicks={150}
          minZoom={0.2}
          maxZoom={8}
          onEngineStop={() => {
            if (fitted.current) return;
            fitted.current = true;
            fg.current?.zoomToFit(400, FIT_PADDING, (node) => visible(node as GraphNode));
          }}
          onNodeClick={(node) => onSelect((node as GraphNode).id)}
          onNodeHover={(node) => setHovered((node as GraphNode | null) ?? null)}
          onBackgroundClick={() => onSelect(null)}
        />
      )}

      {/* Decorative for assistive tech: the same facts are in the search list and the inspector. */}
      <div
        ref={tooltip}
        aria-hidden="true"
        className={`pointer-events-none absolute left-0 top-0 max-w-64 rounded-md border border-slate-200 bg-white px-3 py-2 text-left ${
          hovered ? "block" : "hidden"
        }`}
      >
        {hovered && (
          <>
            <span className="block truncate text-sm font-semibold text-slate-900">{displayName(hovered)}</span>
            <span className="mt-0.5 flex items-center gap-1.5 text-xs text-slate-600">
              <span className="block size-2 shrink-0 rounded-full" style={{ background: groupInfo(groupOf(hovered.type)).color }} />
              <span className="truncate">{hovered.type || "no type"}</span>
              <span aria-hidden="true">·</span>
              <span className="shrink-0 tabular-nums">
                {hoveredCount} relation{hoveredCount === 1 ? "" : "s"}
              </span>
            </span>
          </>
        )}
      </div>
    </div>
  );
}
