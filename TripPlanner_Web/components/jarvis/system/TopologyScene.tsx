"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-3d";
import { FogExp2, Vector3, type Object3D, type PerspectiveCamera } from "three";
import type { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  CATEGORY_COLORS,
  CORE_ID,
  endId,
  neighbourMap,
  OBSIDIAN,
  shownUnder,
  STATUS_LABEL,
  WARNING,
  type ProviderKind,
  type ProviderStatus,
  type TopoGraph,
  type TopoLink,
  type TopoNode,
  type TopologyFilter,
} from "@/lib/jarvis/system";
import {
  animate,
  createLinkVisual,
  createParticle,
  createTopoRings,
  createTopoVisual,
  disposeRings,
  placeLink,
  placeTopoLabels,
  styleLink,
  styleTopoVisual,
  type LinkVisual,
  type TopoStyle,
  type TopoVisual,
} from "./topologyObjects";

/**
 * The System Topology page's 3D scene (react-force-graph-3d on Three.js). Loaded only in the
 * browser: SystemView imports it through next/dynamic with ssr: false (WebGL and the library
 * need `window`).
 *
 *   space     obsidian black (#050508) under exponential fog; a faint ring for the providers'
 *             orbit and fainter dashed ones for the tools' rows
 *   nodes     built once each (topologyObjects.ts) where lib/jarvis/system.ts pinned them, then
 *             restyled in place as statuses, focus, the filter, the search and labels change
 *   links     Jarvis to each provider, each provider to its tools, an agent to the tool servers it
 *             works through. Where both ends work, glowing particles flow along the line (Jarvis
 *             out to its providers, agents out to their servers); where one cannot, the line is
 *             slate, dashed and still. Reduced motion stops the particles where they are.
 *   motion    the core's icosahedron and the globes turn slowly; amber warning halos breathe;
 *             after a few idle seconds the camera orbits the system. A press, a drag, the wheel,
 *             pointing at a node or opening one stops the orbit at once. Reduced motion stops all.
 *   camera    opening a node (here or in the panel) flies the camera to it; Fit to view frames
 *             everything shown, in the part of the screen the panels leave clear.
 *
 * The library's own tooltips are switched off (nodeLabel/linkLabel give ""): they write HTML, and
 * tool names and descriptions come from tool servers. This component's tooltip is plain text.
 */

type FGNode = NodeObject<TopoNode>;
type FGLink = LinkObject<TopoNode, TopoLink>;
type Graph = ForceGraphMethods<FGNode, FGLink>;

export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface TopologySceneApi {
  fit: (durationMs?: number) => void;
  focus: (id: string) => void;
}

/** What the tooltip says about a node. */
export interface NodeInfo {
  title: string;
  detail: string;
  status: ProviderStatus;
  color: string;
  note?: string;
}

export interface TopologySceneProps {
  graph: TopoGraph;
  /** Bumped when statuses change in place (a refresh with the same shape). */
  revision: number;
  selectedId: string | null;
  matchIds: ReadonlySet<string> | null;
  filter: TopologyFilter;
  /** Each provider's kind, by provider id (the filter works on it). */
  kinds: ReadonlyMap<string, ProviderKind>;
  info: ReadonlyMap<string, NodeInfo>;
  labels: boolean;
  insets: () => Insets;
  onSelect: (id: string | null) => void;
  onReady: (api: TopologySceneApi) => void;
}

const SPIN = 0.0045;
const IDLE_MS = 4500;
const ORBIT_SPEED = 0.32;
// The camera looks down on the orbital plane at this angle - steeper on a portrait screen, so the
// orbit fills its height instead of lying flat across the middle.
const ELEVATION = (34 * Math.PI) / 180;
const ELEVATION_PORTRAIT = (60 * Math.PI) / 180;
const NARROW = 640;
const elevationFor = (size: { width: number; height: number } | null) =>
  size && size.height > size.width * 1.15 ? ELEVATION_PORTRAIT : ELEVATION;
const FIT_PADDING = 24;
const FOG_REACH = 0.7;
const PARTICLE_SPEED = { provider: 0.0055, uses: 0.0035, capability: 0 } as const;
const TEXT_CORE = "#D6F6FF";
const TEXT_PROVIDER = "#F1F5FB";
const TEXT_CAPABILITY = "#A9B6CC";

const motion = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1);

function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

export default function TopologyScene({
  graph,
  revision,
  selectedId,
  matchIds,
  filter,
  kinds,
  info,
  labels,
  insets,
  onSelect,
  onReady,
}: TopologySceneProps) {
  const box = useRef<HTMLDivElement | null>(null);
  const tooltip = useRef<HTMLDivElement | null>(null);
  const fg = useRef<Graph | undefined>(undefined);
  const visuals = useRef(new Map<TopoNode, TopoVisual>());
  const linkVisuals = useRef(new Map<string, LinkVisual>());
  const styleNode = useRef<(node: TopoNode, visual: TopoVisual) => void>(() => {});
  const styleOneLink = useRef<(link: TopoLink, visual: LinkVisual) => void>(() => {});
  const fitted = useRef(false);
  const lastTouch = useRef(0);
  const busy = useRef(false);
  const offset = useRef({ x: 0, y: 0, tx: 0, ty: 0 });
  const renderSize = useRef({ width: 1, height: 1 });
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [hovered, setHovered] = useState<TopoNode | null>(null);
  const [webgl, setWebgl] = useState<boolean | null>(null);
  const [reduced, setReduced] = useState(false);
  const mounted = size !== null && webgl === true;

  useEffect(() => {
    if (size) renderSize.current = size;
  }, [size]);

  useEffect(() => {
    busy.current = hovered !== null || selectedId !== null;
    if (busy.current) lastTouch.current = performance.now();
  }, [hovered, selectedId]);

  useEffect(() => {
    const probe = document.createElement("canvas");
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setWebgl(!!(probe.getContext("webgl2") ?? probe.getContext("webgl")));
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(query.matches);
    const onChange = () => setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      setSize((current) => (current && current.width === width && current.height === height ? current : { width, height }));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // The view's centre follows the part of the screen the panels leave clear.
  useEffect(() => {
    if (!mounted) return;
    const update = () => {
      const clear = insets();
      offset.current.tx = (clear.right - clear.left) / 2;
      offset.current.ty = (clear.bottom - clear.top) / 2;
    };
    update();
    const observer = new ResizeObserver(update);
    document.querySelectorAll("[data-system-topbar], [data-system-panel]").forEach((element) => observer.observe(element));
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [mounted, insets]);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);
  const neighbours = useMemo(() => neighbourMap(graph), [graph]);

  // ---- who is shown, lit, matched ---------------------------------------------------------------

  const visible = useCallback(
    (node: TopoNode) => node.id === CORE_ID || shownUnder(filter, kinds.get(node.providerId) ?? "agent"),
    [filter, kinds],
  );
  const linkVisible = useCallback(
    (link: TopoLink) => {
      const s = nodeById.get(endId(link.source));
      const t = nodeById.get(endId(link.target));
      return !!s && !!t && visible(s) && visible(t);
    },
    [nodeById, visible],
  );
  const focusId = hovered?.id ?? selectedId;
  // The open (or pointed-at) node and everything it is linked to.
  const lit = useMemo(
    () => (focusId ? new Set([focusId, ...(neighbours.get(focusId) ?? [])]) : null),
    [focusId, neighbours],
  );

  // ---- the look of every node ------------------------------------------------------------------------

  const styleOf = useCallback(
    (node: TopoNode): TopoStyle => {
      const selected = node.id === selectedId;
      const hover = hovered?.id === node.id;
      const inFocus = lit !== null && lit.has(node.id);
      const matched = matchIds === null || matchIds.has(node.id);
      const behind = filter === "capabilities" && node.kind === "provider";
      const strength = lit !== null && !inFocus ? 0.14 : !matched ? 0.16 : behind ? 0.45 : 1;
      const important = selected || hover || inFocus || (matchIds !== null && matchIds.has(node.id));
      // Floating labels on: Jarvis and every provider are named; tools too while nothing is open
      // (or under the Capabilities filter), where there is room. Off: only what is open or found.
      let named: boolean;
      if (important) named = true;
      else if (!labels || strength < 0.45) named = false;
      else if (node.kind !== "capability") named = true;
      else named = filter === "capabilities" || lit === null;
      return {
        visible: visible(node),
        color: CATEGORY_COLORS[node.category],
        status: node.status,
        strength,
        selected,
        hovered: hover,
        label: named
          ? {
              text: clip(node.label, node.kind === "capability" ? 22 : 24),
              color: node.kind === "core" ? TEXT_CORE : node.kind === "provider" || important ? TEXT_PROVIDER : TEXT_CAPABILITY,
              weight: node.kind === "core" ? 700 : node.kind === "provider" || selected ? 600 : 500,
              px: node.kind === "core" ? 14 : node.kind === "provider" ? 12 : 11,
              priority: node.kind === "core" ? 0 : important ? 1 : node.kind === "provider" ? 2 : 3,
            }
          : null,
      };
    },
    // revision: statuses changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedId, hovered, lit, matchIds, labels, filter, visible, revision],
  );

  const pixelUnit = useCallback(() => {
    const camera = fg.current?.camera() as PerspectiveCamera | undefined;
    if (!camera || !size) return 0.002;
    return (2 * Math.tan((camera.fov * Math.PI) / 360)) / size.height;
  }, [size]);

  useEffect(() => {
    styleNode.current = (node, visual) => styleTopoVisual(visual, styleOf(node), pixelUnit());
    for (const node of graph.nodes) {
      const visual = visuals.current.get(node);
      if (visual) styleTopoVisual(visual, styleOf(node), pixelUnit());
    }
  }, [graph, styleOf, pixelUnit]);

  // One object per node object, made when the library first asks for it. A new topology shape is
  // a new graph with new node objects, so it gets new objects: the library disposes the old ones.
  // The accessor never changes, so within one graph nothing is ever rebuilt.
  const nodeObject = useCallback((raw: FGNode) => {
    const node = raw as TopoNode;
    let visual = visuals.current.get(node);
    if (!visual) {
      visual = createTopoVisual(node.id, node.kind, node.radius);
      visuals.current.set(node, visual);
      styleNode.current(node, visual);
    }
    return visual.group;
  }, []);
  useEffect(() => {
    const keep = new Set(graph.nodes);
    for (const node of visuals.current.keys()) if (!keep.has(node)) visuals.current.delete(node);
  }, [graph]);

  // ---- links ------------------------------------------------------------------------------------------

  const linkStrength = useCallback(
    (link: TopoLink) => {
      const s = endId(link.source);
      const t = endId(link.target);
      if (focusId !== null) return s === focusId || t === focusId || (lit?.has(s) && lit?.has(t) && link.kind !== "provider") ? 1 : 0.12;
      if (matchIds !== null && (!matchIds.has(s) || !matchIds.has(t))) return 0.15;
      return 1;
    },
    [focusId, lit, matchIds],
  );

  useEffect(() => {
    styleOneLink.current = (link, visual) => styleLink(visual, link.kind, CATEGORY_COLORS[link.category], linkStrength(link));
    for (const link of graph.links) {
      const visual = linkVisuals.current.get(link.id);
      if (visual) styleOneLink.current(link, visual);
    }
  }, [graph, linkStrength, revision]);

  const linkObject = useCallback((raw: FGLink) => {
    const link = raw as TopoLink;
    const visual = createLinkVisual(link.active);
    visual.line.userData.linkId = link.id;
    linkVisuals.current.set(link.id, visual);
    styleOneLink.current(link, visual);
    return visual.line;
  }, []);

  const linkPosition = useCallback((object: Object3D, { start, end }: { start: { x: number; y: number; z: number }; end: { x: number; y: number; z: number } }) => {
    const visual = linkVisuals.current.get(String(object.userData.linkId ?? ""));
    if (visual && visual.line === object) placeLink(visual, start, end);
    return true;
  }, []);

  // Data flows where both ends work: two particles out to each provider, one from an agent to a server.
  const particles = useCallback((raw: FGLink) => {
    const link = raw as TopoLink;
    return link.active ? (link.kind === "provider" ? 2 : link.kind === "uses" ? 1 : 0) : 0;
  }, []);
  const particleSpeed = useCallback(
    (raw: FGLink) => (reduced ? 0 : PARTICLE_SPEED[(raw as TopoLink).kind]),
    [reduced],
  );
  const particleObject = useCallback((raw: FGLink) => {
    const link = raw as TopoLink;
    return createParticle(CATEGORY_COLORS[link.category], link.kind === "provider" ? 2.2 : 1.6);
  }, []);

  // ---- the scene: fog, rings ----------------------------------------------------------------------------

  useEffect(() => {
    const api = fg.current;
    if (!mounted || !api) return;
    const scene = api.scene();
    scene.fog = new FogExp2(OBSIDIAN, 0.001);
    const rings = createTopoRings(graph.rings);
    scene.add(rings);
    return () => {
      scene.remove(rings);
      disposeRings(rings);
    };
  }, [mounted, graph]);

  // ---- the camera ----------------------------------------------------------------------------------------

  const fit = useCallback(
    (durationMs = 800) => {
      const api = fg.current;
      if (!api || !size) return;
      const shown = graph.nodes.filter((node) => visible(node));
      if (!shown.length) return;
      const camera = api.camera() as PerspectiveCamera;
      const controls = api.controls() as OrbitControls;
      const lo = new Vector3(Infinity, Infinity, Infinity);
      const hi = new Vector3(-Infinity, -Infinity, -Infinity);
      for (const node of shown) {
        lo.min(new Vector3(node.x, node.y, node.z));
        hi.max(new Vector3(node.x, node.y, node.z));
      }
      const centre = lo.clone().add(hi).multiplyScalar(0.5);
      const from = camera.position.clone().sub(controls.target);
      const heading = Math.hypot(from.x, from.z) > 1e-6 ? Math.atan2(from.x, from.z) : 0;
      const elevation = elevationFor(size);
      const dir = new Vector3(Math.sin(heading) * Math.cos(elevation), Math.sin(elevation), Math.cos(heading) * Math.cos(elevation));
      const right = new Vector3().crossVectors(new Vector3(0, 1, 0), dir).normalize();
      const up = new Vector3().crossVectors(dir, right).normalize();
      const clear = insets();
      const width = Math.max(120, size.width - clear.left - clear.right - 2 * FIT_PADDING);
      const height = Math.max(120, size.height - clear.top - clear.bottom - 2 * FIT_PADDING);
      const t = Math.tan((camera.fov * Math.PI) / 360);
      let distance = 1;
      let reach = 1;
      for (const node of shown) {
        const p = new Vector3(node.x, node.y, node.z).sub(centre);
        const r = node.radius * 1.6 + 10;
        reach = Math.max(reach, p.length() + r);
        const depth = p.dot(dir);
        distance = Math.max(
          distance,
          depth + ((Math.abs(p.dot(right)) + r) * size.height) / (t * width),
          depth + ((Math.abs(p.dot(up)) + r) * size.height) / (t * height),
        );
      }
      api.cameraPosition(centre.clone().add(dir.multiplyScalar(distance)), centre, durationMs * motion());
      const fog = api.scene().fog;
      if (fog instanceof FogExp2) fog.density = FOG_REACH / (distance + reach);
    },
    [graph, size, visible, insets],
  );

  const focus = useCallback(
    (id: string) => {
      const api = fg.current;
      const node = nodeById.get(id);
      if (!api || !node) return;
      const camera = api.camera() as PerspectiveCamera;
      const controls = api.controls() as OrbitControls;
      const at = new Vector3(node.x, node.y, node.z);
      // Look from outside the system towards the node, above the plane, so its tools and Jarvis
      // behind it stay in view.
      const outward = node.kind === "core" ? camera.position.clone().sub(controls.target).setY(0) : at.clone().setY(0);
      if (outward.lengthSq() < 1e-6) outward.set(0, 0, 1);
      const elevation = Math.min(elevationFor(size) + 0.15, 1.3);
      const dir = outward.normalize().multiplyScalar(Math.cos(elevation)).setY(Math.sin(elevation)).normalize();
      const clear = insets();
      const height = size?.height ?? 800;
      const room = Math.min((size?.width ?? 800) - clear.left - clear.right, height - clear.top - clear.bottom);
      // A provider fills about a twelfth of the clear area (its tools fit round it); a tool, a sixteenth.
      const across = Math.min(130, Math.max(28, room * (node.kind === "capability" ? 0.06 : node.kind === "core" ? 0.14 : 0.085)));
      const t = Math.tan((camera.fov * Math.PI) / 360);
      const distance = (2 * node.radius * height) / (2 * t * across);
      lastTouch.current = performance.now();
      api.cameraPosition(at.clone().add(dir.multiplyScalar(distance)), at, 1100 * motion());
    },
    [nodeById, insets, size],
  );

  useEffect(() => {
    onReady({ fit, focus });
  }, [onReady, fit, focus]);

  const fitRef = useRef(fit);
  useEffect(() => {
    fitRef.current = fit;
  }, [fit]);
  useEffect(() => {
    if (fitted.current) fitRef.current();
  }, [filter]);

  const firstFrame = useCallback(() => {
    if (fitted.current) return;
    fitted.current = true;
    lastTouch.current = performance.now();
    fit(0);
  }, [fit]);

  // ---- one loop: spinning wireframes, breathing halos, the idle orbit, the eased view offset ----------

  useEffect(() => {
    if (!mounted) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    const tick = (time: number) => {
      frame = requestAnimationFrame(tick);
      const api = fg.current;
      if (!api) return;
      if (!reduce.matches) for (const visual of visuals.current.values()) animate(visual, SPIN, time);

      const controls = api.controls() as OrbitControls;
      const idle = !reduce.matches && fitted.current && !busy.current && performance.now() - lastTouch.current > IDLE_MS;
      controls.autoRotate = idle;
      controls.autoRotateSpeed = ORBIT_SPEED;

      const camera = api.camera() as PerspectiveCamera;
      // A narrow screen has no room for every provider's name: they wait for room like the tools'.
      const { width: w, height: h } = renderSize.current;
      placeTopoLabels(visuals.current.values(), camera, w, h, w < NARROW ? 1 : 2);
      const o = offset.current;
      const step = reduce.matches ? 1 : 0.2;
      o.x = Math.abs(o.tx - o.x) < 0.5 ? o.tx : o.x + (o.tx - o.x) * step;
      o.y = Math.abs(o.ty - o.y) < 0.5 ? o.ty : o.y + (o.ty - o.y) * step;
      const { width, height } = renderSize.current;
      const view = camera.view;
      if (!view || view.offsetX !== o.x || view.offsetY !== o.y || view.fullWidth !== width || view.fullHeight !== height) {
        camera.setViewOffset(width, height, o.x, o.y, width, height);
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [mounted]);

  // Any press, drag or wheel stops the idle orbit at once.
  useEffect(() => {
    const element = box.current;
    if (!element || !mounted) return;
    const touch = () => {
      lastTouch.current = performance.now();
      const controls = fg.current?.controls() as OrbitControls | undefined;
      if (controls) controls.autoRotate = false;
    };
    const events = ["pointerdown", "wheel", "touchstart", "keydown"] as const;
    events.forEach((name) => element.addEventListener(name, touch, { capture: true, passive: true }));
    const controls = fg.current?.controls() as OrbitControls | undefined;
    controls?.addEventListener("start", touch);
    return () => {
      events.forEach((name) => element.removeEventListener(name, touch, { capture: true }));
      controls?.removeEventListener("start", touch);
    };
  }, [mounted]);

  // ---- the hover tooltip (text only, moved without re-rendering) -----------------------------------

  // The library keeps raycasting at the last pointer position after the pointer has left (or a
  // finger has lifted), so a node the camera carries under that spot would count as pointed at:
  // the rest of the system would dim and a tooltip appear. Hover counts only while a mouse is over
  // the canvas; a touch never hovers.
  const pointerInside = useRef(false);
  const onNodeHover = useCallback((node: FGNode | null) => {
    setHovered(pointerInside.current ? ((node as TopoNode | null) ?? null) : null);
  }, []);
  const onPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "mouse") pointerInside.current = false;
  }, []);
  const onPointerLeave = useCallback(() => {
    pointerInside.current = false;
    setHovered(null);
  }, []);

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    pointerInside.current = event.pointerType === "mouse";
    const tip = tooltip.current;
    const element = box.current;
    if (!tip || !element) return;
    const rect = element.getBoundingClientRect();
    const x = Math.min(event.clientX - rect.left + 14, rect.width - tip.offsetWidth - 8);
    const y = Math.min(event.clientY - rect.top + 16, rect.height - tip.offsetHeight - 8);
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }, []);

  const hoveredInfo = hovered ? info.get(hovered.id) : undefined;

  return (
    <div
      ref={box}
      className="absolute inset-0 overflow-hidden"
      onPointerMove={onPointerMove}
      onPointerDownCapture={onPointerDown}
      onPointerLeave={onPointerLeave}
    >
      {webgl === false && (
        <div className="absolute inset-0 grid place-items-center px-6 pt-24 text-center">
          <p className="max-w-sm text-sm leading-6 text-[#c9d3e3]">
            The 3D view needs WebGL, which this browser has switched off. Every provider and tool is still in the panel&apos;s list.
          </p>
        </div>
      )}
      {mounted && size && (
        <ForceGraph3D<TopoNode, TopoLink>
          ref={fg}
          graphData={graph}
          width={size.width}
          height={size.height}
          backgroundColor={OBSIDIAN}
          showNavInfo={false}
          controlType="orbit"
          nodeId="id"
          nodeThreeObject={nodeObject}
          nodeThreeObjectExtend={false}
          nodeLabel={() => ""}
          linkLabel={() => ""}
          linkThreeObject={linkObject}
          linkThreeObjectExtend={false}
          linkPositionUpdate={linkPosition}
          linkVisibility={(link) => linkVisible(link as TopoLink)}
          linkDirectionalParticles={particles}
          linkDirectionalParticleSpeed={particleSpeed}
          linkDirectionalParticleThreeObject={particleObject}
          enableNodeDrag={false}
          warmupTicks={0}
          cooldownTicks={0}
          onEngineStop={firstFrame}
          onNodeClick={(node) => onSelect((node as TopoNode).id)}
          onNodeHover={onNodeHover}
          onBackgroundClick={() => onSelect(null)}
        />
      )}

      {/* Decorative for assistive tech: the same facts are in the panel. */}
      <div
        ref={tooltip}
        aria-hidden="true"
        className={`pointer-events-none absolute left-0 top-0 z-10 max-w-72 rounded-xl border border-white/10 bg-[#0b1020]/90 px-3 py-2 text-left shadow-[0_8px_28px_rgba(0,0,0,0.5)] backdrop-blur-md ${
          hovered && hoveredInfo ? "block" : "hidden"
        }`}
      >
        {hovered && hoveredInfo && (
          <>
            <span className="block truncate text-sm font-semibold text-[#f1f5fb]">{hoveredInfo.title}</span>
            <span className="mt-0.5 flex items-center gap-1.5 text-xs text-[#c9d3e3]">
              <span aria-hidden="true" className="block size-2 shrink-0 rounded-full" style={{ background: hoveredInfo.color }} />
              <span className="truncate">{hoveredInfo.detail}</span>
              <span aria-hidden="true">·</span>
              <span
                className="shrink-0 font-semibold"
                style={{ color: hoveredInfo.status === "online" ? "#34d399" : hoveredInfo.status === "auth_required" ? WARNING : "#a3b0c6" }}
              >
                {STATUS_LABEL[hoveredInfo.status]}
              </span>
            </span>
            {hoveredInfo.note && <span className="mt-1 line-clamp-2 text-xs leading-5 text-[#a3b0c6]">{hoveredInfo.note}</span>}
          </>
        )}
      </div>
    </div>
  );
}
