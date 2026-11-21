"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-3d";
import { FogExp2, Vector3, type PerspectiveCamera } from "three";
import type { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { displayName, groupOf, type TypeGroupId } from "@/lib/jarvis/memory";
import { isPlaced, nodeRadius } from "@/lib/jarvis/memoryLayout";
import {
  colorOf,
  endId,
  ENTRY_STATUS_LABEL,
  OBSIDIAN,
  orbitWord,
  type GraphLink,
  type GraphNode,
  type Looks,
  type Orbits,
  type SolarGraph,
} from "@/lib/jarvis/memoryOrbits";
import {
  createFallbackObject,
  createRings,
  createVisual,
  disposeRings,
  placeLabels,
  spin,
  styleVisual,
  type NodeVisual,
  type VisualStyle,
} from "./holoObjects";

/**
 * The Memory page's 3D holographic solar system (react-force-graph-3d on Three.js). Loaded only
 * in the browser: MemoryView imports it through next/dynamic with ssr: false (WebGL and the
 * library need `window`).
 *
 *   space     obsidian black (#06070B) with exponential fog, so the far side of the system fades
 *             into the dark; one crisp ring per orbit on the orbital plane
 *   entries   built once per entry (holoObjects.ts) at the place lib/jarvis/memoryLayout.ts
 *             pinned it - nothing drifts, so everything is easy to point at - and restyled in
 *             place as colours, statuses, focus and labels change
 *   motion    one animation loop: the Sun's and the planets' wireframe globes turn slowly on their
 *             own axes (0.004 rad a frame) and, after a few idle seconds, the camera orbits the
 *             system; a press, a drag, the wheel, pointing at an entry or opening one stops the
 *             orbit at once. Reduced motion stops both.
 *   camera    opening an entry (here or in the panel) flies the camera to it; Fit to view frames
 *             everything shown. The view's centre is the part of the screen the panels leave
 *             clear (a camera view offset, eased as the panel or sheet changes).
 *   links     hairlines: blue at 22% for a fact that holds, fainter slate for one that ended;
 *             the open or pointed-at entry's relations light up and the rest of the system dims
 *
 * The library's own tooltips are switched off (`nodeLabel`/`linkLabel` give ""): they write
 * their text as HTML, and names come from what people said. This component's tooltip is plain text.
 */

type FGNode = NodeObject<GraphNode>;
type FGLink = LinkObject<GraphNode, GraphLink>;
type Graph = ForceGraphMethods<FGNode, FGLink>;

/** How much of the canvas the page's floating panels cover, in CSS pixels. */
export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface HoloSceneApi {
  /** Frames every shown entry in the part of the screen the panels leave clear. */
  fit: (durationMs?: number) => void;
  /** Flies the camera to one entry. */
  focus: (id: string) => void;
}

export interface HoloSceneProps {
  /** Same object until entries or relations are added or removed (positions are pinned in it). */
  graph: SolarGraph;
  /** computeOrbits(graph): each entry's orbit, the rings, the neighbours. */
  orbits: Orbits;
  /** computeLooks(graph, orbits): each entry's colour family and status. */
  looks: Looks;
  /** Bumped when an entry's words or a relation's status change in place. */
  revision: number;
  selectedId: string | null;
  /** The search's matches, or null when there is no search. */
  matchIds: ReadonlySet<string> | null;
  /** "all", or the one type group to show (the centre always stays). */
  group: TypeGroupId | "all";
  /** Floating labels: the Sun's and the planets' names always; moons' when lit up. */
  labels: boolean;
  /** Measured when needed: what the top bar, the panel and the sheet cover right now. */
  insets: () => Insets;
  onSelect: (id: string | null) => void;
  onReady: (api: HoloSceneApi) => void;
}

const LINK = { active: "rgba(100, 150, 255, 0.22)", completed: "rgba(148, 163, 184, 0.15)", archived: "rgba(148, 163, 184, 0.08)" };
const LINK_LIT = "rgba(150, 195, 255, 0.95)";
const LINK_LIT_ENDED = "rgba(190, 205, 225, 0.6)";
const LINK_DARK = "rgba(100, 150, 255, 0.035)";
const TEXT_SUN = "#FFF3C4";
const TEXT_PLANET = "#F1F5FB";
const TEXT_MOON = "#A3B0C6";

// Radians a frame the wireframe globes turn on their own axes.
const SPIN = 0.004;
// Quiet this long, nothing pointed at or open: the camera starts its slow orbit.
const IDLE_MS = 4500;
// OrbitControls' autoRotateSpeed: one turn in about three minutes.
const ORBIT_SPEED = 0.35;
// The camera looks down on the orbital plane at this angle.
const ELEVATION = (50 * Math.PI) / 180;
const FIT_PADDING = 28;
// Fog: the far side of the system keeps about 57% of its light.
const FOG_REACH = 0.75;

/** 0 under reduced motion (camera moves jump), else 1. */
const motion = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1);

// Failures already logged: a fault that happens every frame is reported once, not 60 times a second.
const reported = new Set<string>();
function reportOnce(what: string, error: unknown): void {
  if (reported.has(what)) return;
  reported.add(what);
  console.error(`Memory 3D view: ${what}; the rest of the scene is still drawn.`, error);
}

function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

export default function HoloScene({
  graph,
  orbits,
  looks,
  revision,
  selectedId,
  matchIds,
  group,
  labels,
  insets,
  onSelect,
  onReady,
}: HoloSceneProps) {
  const box = useRef<HTMLDivElement | null>(null);
  const tooltip = useRef<HTMLDivElement | null>(null);
  const fg = useRef<Graph | undefined>(undefined);
  // One visual per entry and shape (kind + size): a new shape is a new object.
  const visuals = useRef(new Map<string, NodeVisual>());
  // The node object each visual was handed to the library for. The library tracks its objects by
  // the node OBJECT, not the id: given a fresh copy of the same entry (the graph loaded again), it
  // builds for the copy and then removes - scene.remove + dispose - the object of the copy it
  // forgot. Had the copy been handed that same object, every entry would leave the scene (only
  // the rings stay) and the library's next clean-up would throw "Cannot convert undefined or null
  // to object" at Map.forEach. So a visual is reused only for the node object it was made for.
  const owners = useRef(new Map<string, GraphNode>());
  // Styles a freshly built visual the way the latest render would (the library builds them later).
  const styleNode = useRef<(node: GraphNode, visual: NodeVisual) => void>(() => {});
  const orbitsRef = useRef(orbits);
  const fitted = useRef(false);
  const lastTouch = useRef(0);
  const busy = useRef(false);
  // The camera's view offset: where it is (x, y) and where it is easing to (tx, ty).
  const offset = useRef({ x: 0, y: 0, tx: 0, ty: 0, w: 0, h: 0 });
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [hovered, setHovered] = useState<GraphNode | null>(null);
  const [webgl, setWebgl] = useState<boolean | null>(null);
  const mounted = size !== null && webgl === true;
  // The animation loop reads the size and the centre without re-subscribing.
  const renderSize = useRef({ width: 1, height: 1 });
  const centreRef = useRef<GraphNode | null>(null);

  useEffect(() => {
    orbitsRef.current = orbits;
  }, [orbits]);

  useEffect(() => {
    if (size) renderSize.current = size;
  }, [size]);

  useEffect(() => {
    centreRef.current = graph.nodes.find((node) => node.id === orbits.centerId) ?? null;
  }, [graph, orbits]);

  useEffect(() => {
    busy.current = hovered !== null || selectedId !== null;
    if (busy.current) lastTouch.current = performance.now();
  }, [hovered, selectedId]);

  // WebGL or a plain message: the panel's list still works without it.
  useEffect(() => {
    const probe = document.createElement("canvas");
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setWebgl(!!(probe.getContext("webgl2") ?? probe.getContext("webgl")));
  }, []);

  // The canvas fills its box; the library needs the size in numbers.
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
    document.querySelectorAll("[data-memory-topbar], [data-memory-panel]").forEach((element) => observer.observe(element));
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [mounted, insets]);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);

  // ---- who is shown, lit, matched ---------------------------------------------------------------

  const visible = useCallback(
    (node: GraphNode) => node.id === orbits.centerId || group === "all" || groupOf(node.type) === group,
    [group, orbits.centerId],
  );
  const linkVisible = useCallback(
    (link: GraphLink) => {
      const s = nodeById.get(endId(link.source));
      const t = nodeById.get(endId(link.target));
      return !!s && !!t && visible(s) && visible(t);
    },
    [nodeById, visible],
  );
  const focusId = hovered?.id ?? selectedId;
  const lit = useMemo(
    () => (focusId ? new Set([focusId, ...(orbits.neighbours.get(focusId) ?? [])]) : null),
    [focusId, orbits],
  );
  const touches = useCallback(
    (link: GraphLink) => focusId !== null && (endId(link.source) === focusId || endId(link.target) === focusId),
    [focusId],
  );

  // ---- the look of every entry --------------------------------------------------------------------

  const styleOf = useCallback(
    (node: GraphNode): VisualStyle => {
      const kind = orbits.byId.get(node.id)?.kind ?? "moon";
      const selected = node.id === selectedId;
      const hover = hovered?.id === node.id;
      const inFocus = lit !== null && lit.has(node.id);
      const matched = matchIds === null || matchIds.has(node.id);
      const strength = lit !== null && !inFocus ? 0.12 : !matched ? 0.16 : 1;
      const important = selected || hover || inFocus || (matchIds !== null && matchIds.has(node.id));
      const named = strength === 1 && (important || (labels && kind !== "moon"));
      return {
        visible: visible(node),
        color: colorOf(node.id, orbits, looks),
        state: kind === "sun" ? "active" : (looks.status.get(node.id) ?? "active"),
        strength,
        selected,
        hovered: hover,
        label: named
          ? {
              text: clip(displayName(node), kind === "sun" ? 26 : 18),
              color: kind === "sun" ? TEXT_SUN : kind === "planet" || important ? TEXT_PLANET : TEXT_MOON,
              weight: kind === "sun" || selected ? 600 : 500,
              px: kind === "sun" ? 13 : kind === "planet" ? 12 : 11,
              priority: kind === "sun" ? 0 : important ? 1 : kind === "planet" ? 2 : 3,
            }
          : null,
      };
    },
    // revision: an entry's words changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [orbits, looks, selectedId, hovered, lit, matchIds, labels, visible, revision],
  );

  /** CSS pixels -> label sprite units (labels keep their size on screen at any distance). */
  const pixelUnit = useCallback(() => {
    const camera = fg.current?.camera() as PerspectiveCamera | undefined;
    if (!camera || !size) return 0.002;
    return (2 * Math.tan((camera.fov * Math.PI) / 360)) / size.height;
  }, [size]);

  useEffect(() => {
    styleNode.current = (node, visual) => styleVisual(visual, styleOf(node), pixelUnit());
    for (const node of graph.nodes) {
      const visual = visuals.current.get(`${node.id}|${shapeOf(node, orbits)}`);
      if (!visual) continue;
      try {
        styleVisual(visual, styleOf(node), pixelUnit());
      } catch (error) {
        // One entry that cannot be restyled keeps its last look; the others are still restyled.
        reportOnce("an entry could not be restyled", error);
      }
    }
  }, [graph, orbits, styleOf, pixelUnit]);

  // A new shape (an entry that changed orbit or size after a delete) rebuilds the objects.
  const shapes = useMemo(() => graph.nodes.map((node) => `${node.id}|${shapeOf(node, orbits)}`).join(","), [graph, orbits]);
  const nodeObject = useCallback(
    (raw: FGNode) => {
      const node = raw as GraphNode;
      const current = orbitsRef.current;
      const key = `${node.id}|${shapeOf(node, current)}`;
      let visual = owners.current.get(key) === node ? visuals.current.get(key) : undefined;
      if (!visual) {
        try {
          visual = createVisual(node.id, current.byId.get(node.id)?.kind ?? "moon", nodeRadius(node, current));
          styleNode.current(node, visual);
        } catch (error) {
          // An exception here would stop the library building every entry after this one (only the
          // rings would be left): this entry becomes a plain sphere instead, and the rest carry on.
          reportOnce("an entry could not be built", error);
          return createFallbackObject(node.id, nodeRadius(node, current));
        }
        visuals.current.set(key, visual);
        owners.current.set(key, node);
      }
      return visual.group;
    },
    // shapes: a new shape must be a new object - the library disposes the ones it replaces.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shapes],
  );
  useEffect(() => {
    // Forget visuals whose entry or shape is gone (the library has already disposed them).
    const keep = new Set(shapes.split(","));
    for (const key of visuals.current.keys()) {
      if (keep.has(key)) continue;
      visuals.current.delete(key);
      owners.current.delete(key);
    }
  }, [shapes]);

  // ---- links ----------------------------------------------------------------------------------------

  const linkColor = useCallback(
    (raw: FGLink) => {
      const link = raw as GraphLink;
      const holds = link.status === "active";
      if (focusId !== null) return touches(link) ? (holds ? LINK_LIT : LINK_LIT_ENDED) : LINK_DARK;
      if (matchIds !== null && (!matchIds.has(endId(link.source)) || !matchIds.has(endId(link.target)))) return LINK_DARK;
      return LINK[link.status] ?? LINK.active;
    },
    // revision: a status changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [focusId, touches, matchIds, revision],
  );

  // ---- the scene: fog, rings ----------------------------------------------------------------------------

  useEffect(() => {
    const api = fg.current;
    if (!mounted || !api) return;
    const scene = api.scene();
    // Made once: fit() tunes its density to the camera's distance, and a new fog here (new orbits
    // after a reload or a delete) would sink the whole system into the dark until the next fit.
    if (!(scene.fog instanceof FogExp2)) scene.fog = new FogExp2(OBSIDIAN, 0.0012);
    const rings = createRings(orbits);
    scene.add(rings);
    return () => {
      scene.remove(rings);
      disposeRings(rings);
    };
  }, [mounted, orbits]);

  // ---- the camera ----------------------------------------------------------------------------------------

  const fit = useCallback(
    (durationMs = 800) => {
      const api = fg.current;
      if (!api || !size) return;
      // Only entries with a real place: one NaN here would make the camera (and the fog) NaN too,
      // and then nothing but the rings would be left on screen.
      const shown = graph.nodes.filter((node) => visible(node) && isPlaced(node));
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
      // Keep the camera's heading round the system; look down at the standard angle.
      const from = camera.position.clone().sub(controls.target);
      const heading = Math.hypot(from.x, from.z) > 1e-6 ? Math.atan2(from.x, from.z) : 0;
      const dir = new Vector3(Math.sin(heading) * Math.cos(ELEVATION), Math.sin(ELEVATION), Math.cos(heading) * Math.cos(ELEVATION));
      const right = new Vector3().crossVectors(new Vector3(0, 1, 0), dir).normalize();
      const up = new Vector3().crossVectors(dir, right).normalize();
      // The nearest distance at which every entry fits the clear part of the screen.
      const clear = insets();
      const width = Math.max(120, size.width - clear.left - clear.right - 2 * FIT_PADDING);
      const height = Math.max(120, size.height - clear.top - clear.bottom - 2 * FIT_PADDING);
      const t = Math.tan((camera.fov * Math.PI) / 360);
      let distance = 1;
      let reach = 1;
      for (const node of shown) {
        const p = new Vector3(node.x, node.y, node.z).sub(centre);
        const r = nodeRadius(node, orbits) * 1.5 + 6;
        reach = Math.max(reach, p.length() + r);
        const depth = p.dot(dir);
        distance = Math.max(
          distance,
          depth + ((Math.abs(p.dot(right)) + r) * size.height) / (t * width),
          depth + ((Math.abs(p.dot(up)) + r) * size.height) / (t * height),
        );
      }
      // Never move the camera somewhere it cannot be: the view stays as it is instead.
      if (![centre.x, centre.y, centre.z, distance, reach].every(Number.isFinite)) {
        reportOnce("the view could not be framed", new Error("non-finite camera position"));
        return;
      }
      api.cameraPosition(centre.clone().add(dir.multiplyScalar(distance)), centre, durationMs * motion());
      const fog = api.scene().fog;
      const density = FOG_REACH / (distance + reach);
      if (fog instanceof FogExp2 && Number.isFinite(density) && density > 0) fog.density = density;
    },
    [graph, orbits, size, visible, insets],
  );

  const focus = useCallback(
    (id: string) => {
      const api = fg.current;
      const node = nodeById.get(id);
      if (!api || !node || !isPlaced(node)) return;
      const camera = api.camera() as PerspectiveCamera;
      const controls = api.controls() as OrbitControls;
      const dir = camera.position.clone().sub(controls.target).normalize();
      if (!Number.isFinite(dir.x)) dir.set(0, Math.sin(ELEVATION), Math.cos(ELEVATION));
      // Never edge-on to the orbital plane.
      if (dir.y < 0.3) {
        dir.y = 0.3;
        dir.normalize();
      }
      const at = new Vector3(node.x, node.y, node.z);
      // Close enough to read it, far enough to keep its neighbours: about a sixth of the clear
      // part of the screen across (36-140px), whatever the screen.
      const clear = insets();
      const height = size?.height ?? 800;
      const room = Math.min((size?.width ?? 800) - clear.left - clear.right, height - clear.top - clear.bottom);
      const across = Math.min(140, Math.max(36, room * 0.16));
      const t = Math.tan((camera.fov * Math.PI) / 360);
      const distance = (2 * nodeRadius(node, orbits) * height) / (2 * t * across);
      if (!Number.isFinite(distance) || distance <= 0) return;
      lastTouch.current = performance.now();
      api.cameraPosition(at.clone().add(dir.multiplyScalar(distance)), at, 1100 * motion());
    },
    [nodeById, orbits, insets, size],
  );

  useEffect(() => {
    onReady({ fit, focus });
  }, [onReady, fit, focus]);

  // A new type filter frames what it leaves.
  const fitRef = useRef(fit);
  useEffect(() => {
    fitRef.current = fit;
  }, [fit]);
  useEffect(() => {
    if (fitted.current) fitRef.current();
  }, [group]);

  const firstFrame = useCallback(() => {
    if (fitted.current) return;
    fitted.current = true;
    lastTouch.current = performance.now();
    fit(0);
  }, [fit]);

  // ---- one loop: spinning globes, the idle orbit, the eased view offset ---------------------------------

  useEffect(() => {
    if (!mounted) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    const tick = () => {
      frame = requestAnimationFrame(tick);
      const api = fg.current;
      if (!api) return;
      try {
        frameWork(api);
      } catch (error) {
        // The next frame is already asked for: a fault here costs the spin or a label, never the scene.
        reportOnce("a frame could not be finished", error);
      }
    };
    const frameWork = (api: Graph) => {
      if (!reduce.matches) for (const visual of visuals.current.values()) spin(visual, SPIN);

      const controls = api.controls() as OrbitControls;
      const idle = !reduce.matches && fitted.current && !busy.current && performance.now() - lastTouch.current > IDLE_MS;
      controls.autoRotate = idle;
      controls.autoRotateSpeed = ORBIT_SPEED;

      const camera = api.camera() as PerspectiveCamera;
      const centre = centreRef.current;
      const sun = centre && isPlaced(centre) ? new Vector3(centre.x, centre.y, centre.z) : null;
      placeLabels(visuals.current.values(), camera, renderSize.current.width, renderSize.current.height, sun);
      const o = offset.current;
      const step = reduce.matches ? 1 : 0.2;
      o.x = Math.abs(o.tx - o.x) < 0.5 ? o.tx : o.x + (o.tx - o.x) * step;
      o.y = Math.abs(o.ty - o.y) < 0.5 ? o.ty : o.y + (o.ty - o.y) * step;
      const width = renderSize.current.width;
      const height = renderSize.current.height;
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

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const tip = tooltip.current;
    const element = box.current;
    if (!tip || !element) return;
    const rect = element.getBoundingClientRect();
    const x = Math.min(event.clientX - rect.left + 14, rect.width - tip.offsetWidth - 8);
    const y = Math.min(event.clientY - rect.top + 16, rect.height - tip.offsetHeight - 8);
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }, []);

  const hoveredInfo = hovered ? orbits.byId.get(hovered.id) : undefined;
  const hoveredCount = hovered ? (orbits.degree.get(hovered.id) ?? 0) : 0;
  const hoveredStatus = hovered && hoveredInfo?.kind !== "sun" ? (looks.status.get(hovered.id) ?? "active") : "active";
  // A moon names the planet whose family it is in.
  const hoveredFamily =
    hovered && hoveredInfo?.kind === "moon" ? (nodeById.get(looks.family.get(hovered.id) ?? "") ?? null) : null;

  return (
    <div ref={box} className="absolute inset-0 overflow-hidden" onPointerMove={onPointerMove} onPointerLeave={() => setHovered(null)}>
      {webgl === false && (
        <div className="absolute inset-0 grid place-items-center px-6 pt-24 text-center">
          <p className="max-w-sm text-sm leading-6 text-[#c9d3e3]">
            The 3D view needs WebGL, which this browser has switched off. Every entry is still in the list, where you can open and correct it.
          </p>
        </div>
      )}
      {mounted && size && (
        <ForceGraph3D<GraphNode, GraphLink>
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
          linkColor={linkColor}
          linkOpacity={1}
          linkWidth={0}
          linkVisibility={(link) => linkVisible(link as GraphLink)}
          enableNodeDrag={false}
          // Every entry is pinned by the layout: nothing to simulate.
          warmupTicks={0}
          cooldownTicks={0}
          onEngineStop={firstFrame}
          onNodeClick={(node) => onSelect((node as GraphNode).id)}
          onNodeHover={(node) => setHovered((node as GraphNode | null) ?? null)}
          onBackgroundClick={() => onSelect(null)}
        />
      )}

      {/* Decorative for assistive tech: the same facts are in the panel's list and its details. */}
      <div
        ref={tooltip}
        aria-hidden="true"
        className={`pointer-events-none absolute left-0 top-0 z-10 max-w-72 rounded-xl border border-white/10 bg-[#0d1326]/90 px-3 py-2 text-left shadow-[0_8px_28px_rgba(0,0,0,0.5)] backdrop-blur-md ${
          hovered ? "block" : "hidden"
        }`}
      >
        {hovered && (
          <>
            <span className="block truncate text-sm font-semibold text-[#f1f5fb]">{displayName(hovered)}</span>
            <span className="mt-0.5 flex items-center gap-1.5 text-xs text-[#c9d3e3]">
              <span aria-hidden="true" className="block size-2 shrink-0 rounded-full" style={{ background: colorOf(hovered.id, orbits, looks) }} />
              <span className="truncate">{hovered.type || "no type"}</span>
              <span aria-hidden="true">·</span>
              <span className="shrink-0">{orbitWord(hoveredInfo, orbits.centerIsMe)}</span>
              <span aria-hidden="true">·</span>
              <span className="shrink-0 tabular-nums">
                {hoveredCount} relation{hoveredCount === 1 ? "" : "s"}
              </span>
            </span>
            {(hoveredStatus !== "active" || hoveredFamily) && (
              <span className="mt-0.5 flex items-center gap-1.5 text-xs">
                {hoveredStatus === "completed" && <span className="font-semibold text-[#34d399]">✓ {ENTRY_STATUS_LABEL.completed}</span>}
                {hoveredStatus === "archived" && <span className="font-semibold text-[#a3b0c6]">{ENTRY_STATUS_LABEL.archived}</span>}
                {hoveredStatus !== "active" && hoveredFamily && <span aria-hidden="true" className="text-[#c9d3e3]">·</span>}
                {hoveredFamily && <span className="truncate text-[#c9d3e3]">Orbits {displayName(hoveredFamily)}</span>}
              </span>
            )}
            {hovered.description && (
              <span className="mt-1 line-clamp-2 text-xs leading-5 text-[#a3b0c6]">{hovered.description}</span>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** An entry's shape - what its object is built from: its kind and its size. */
function shapeOf(node: GraphNode, orbits: Orbits): string {
  return `${orbits.byId.get(node.id)?.kind ?? "moon"}:${nodeRadius(node, orbits).toFixed(2)}`;
}
