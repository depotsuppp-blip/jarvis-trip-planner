"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods, type LinkObject, type NodeObject } from "react-force-graph-2d";
import { forceCollide, forceRadial, type SimulationNodeDatum } from "d3-force";
import {
  displayName,
  groupOf,
  isMe,
  RELATION_DASH,
  relationLabel,
  type TypeGroupId,
} from "@/lib/jarvis/memory";
import {
  colorOf,
  endId,
  ENTRY_STATUS_LABEL,
  orbitWord,
  SUN_COLORS,
  type GraphLink,
  type GraphNode,
  type Looks,
  type Orbits,
  type SolarGraph,
} from "@/lib/jarvis/memoryOrbits";

/**
 * The memory graph as a solar system, drawn by hand on a canvas (react-force-graph-2d). Loaded
 * only in the browser: MemoryView imports it through next/dynamic with ssr: false (the library
 * touches `window` as it loads).
 *
 *   space     deep #060913 with a still starfield (its own canvas, drawn once per size) and
 *             crisp concentric rings, one per orbit (blue at 22%, 1.2px); the outer orbit's is dotted
 *   the Sun   the person's own entry, pinned at the centre: a golden-amber sphere in a corona,
 *             with a slow breathing aura (a DOM layer animated on the compositor - the canvas
 *             itself stays still - and not at all under reduced motion)
 *   planets   entries one relation away, glowing spheres on the inner orbit, each in its own
 *             signature colour; its moons share it, a little lighter (lib/jarvis/memoryOrbits.ts)
 *   moons     two relations away or more, smaller and softer orbs further out
 *   status    on the orb itself, from its main fact: Active glows at full strength; Done glows
 *             calmer and wears an emerald check badge on its top-right edge and a faint emerald
 *             ring; Archived is greyed, at 35%, inside a dashed ring
 *   relations translucent lines with an arrow at their target; a fact that holds carries a faint
 *             glow, one that ended is dimmer and dashed (completed) or dotted (archived)
 *
 * The layout is still: d3-force's forceRadial holds every entry on its orbit (by hops from the
 * centre), and the simulation settles in warm-up ticks before the first frame, so nothing turns,
 * drifts or circles. Pointing at an entry (or opening it) lights it, its relations and its
 * neighbours and dims the rest.
 *
 * The library's own hover tooltip is switched off (`nodeLabel={() => ""}`): it writes its text
 * as HTML, and names come from what people said. This component's tooltip is plain text.
 */

type FGNode = NodeObject<GraphNode>;
type FGLink = LinkObject<GraphNode, GraphLink>;
type SimNode = FGNode & SimulationNodeDatum;
type Box = [number, number, number, number];

/** How much of the canvas the page's floating panels cover, in CSS pixels. */
export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface SolarCanvasApi {
  /** Frames every visible entry in the part of the canvas the panels leave clear. */
  fit: (durationMs?: number) => void;
  /** Glides to one entry and zooms in on it, centred in what the panels leave clear. */
  focus: (id: string) => void;
}

export interface SolarCanvasProps {
  /** Same object until entries or relations are added or removed (a new one re-runs the layout). */
  graph: SolarGraph;
  /** computeOrbits(graph): each entry's orbit, the rings, the neighbours. */
  orbits: Orbits;
  /** computeLooks(graph, orbits): each entry's colour family and status. */
  looks: Looks;
  /** Bumped when an entry's words or a relation's status change in place, so the canvas repaints. */
  revision: number;
  selectedId: string | null;
  /** The search's matches, or null when there is no search. */
  matchIds: ReadonlySet<string> | null;
  /** "all", or the one type group to show (the centre always stays). */
  group: TypeGroupId | "all";
  /** Floating labels: the Sun's and the planets' names always, the moons' when zoomed in. */
  labels: boolean;
  /** Measured when needed: what the top bar, the panel and the sheet cover right now. */
  insets: () => Insets;
  onSelect: (id: string | null) => void;
  onReady: (api: SolarCanvasApi) => void;
}

const SPACE = "#060913";
const LABEL_HALO = "rgba(6, 9, 19, 0.9)";
const RING = "rgba(120, 160, 255, 0.22)";
const RING_DRIFT = "rgba(120, 160, 255, 0.16)";
const RING_WIDTH = 1.2;
const TEXT_SUN = "#fff3b0";
const TEXT_PLANET = "#f1f5fb";
const TEXT_MOON = "#c9d3e3";
const TEXT_LINK = "#a3b0c6";
const LINK = { active: "rgba(160, 186, 255, 0.34)", completed: "rgba(148, 163, 184, 0.26)", archived: "rgba(148, 163, 184, 0.18)" };
const LINK_LIT = "rgba(226, 236, 255, 0.92)";
const LINK_LIT_ENDED = "rgba(203, 213, 225, 0.62)";
const LINK_DARK = "rgba(148, 163, 184, 0.05)";
const GLOW_NEAR = "rgba(150, 180, 255, 0.22)";
const GLOW_FAR = "rgba(120, 150, 255, 0.075)";
// Done: an emerald badge with a dark check (7.6:1 on the badge), an emerald ring round the orb.
const DONE = "#10B981";
const DONE_INK = "#022C22";
const DONE_RING = "rgba(16, 185, 129, 0.6)";
// Archived: greyed towards slate, inside a dashed ring.
const ARCHIVED_GREY = "#64748B";
const ARCHIVED_RING = "rgba(148, 163, 184, 0.9)";
const ARCHIVED_ALPHA = 0.35;
const FONT = "Geist, Arial, sans-serif";

const SUN_RADIUS = 17;
const CENTRE_RADIUS = 12;
// The least room between two moons on one ring (graph units of arc).
const MOON_GAP = 40;
// The moons' names join the planets' from this zoom.
const MOON_LABEL_ZOOM = 1.5;
// From this zoom every relation shows its words (where they fit without covering another's).
const LINK_LABEL_ZOOM = 2.2;
const FIT_PADDING = 36;
const MIN_ZOOM = 0.12;
const MAX_ZOOM = 8;
const MAX_FIT_ZOOM = 2.4;
const FOCUS_ZOOM = 1.8;
// The aura layer is this many CSS pixels across before it is scaled to the Sun on screen.
const AURA_SIZE = 200;
const AURA_REACH = 8; // aura diameter = Sun radius x zoom x this

/** 0 under reduced motion (camera moves jump), else 1. */
const motion = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1);

function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

function canvasFont(ctx: CanvasRenderingContext2D): string {
  // The CSS variable is not resolved inside a canvas font string: read Geist's real family once.
  const family = getComputedStyle(ctx.canvas).getPropertyValue("--font-geist-sans").trim();
  return family ? `${family}, Arial, sans-serif` : FONT;
}

function rgbOf(hex: string): [number, number, number] {
  return [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16)) as [number, number, number];
}

/** `hex` moved `amount` (0-1) towards `other`, as hex. */
function blend(hex: string, other: string, amount: number): string {
  const b = rgbOf(other);
  return `#${rgbOf(hex)
    .map((value, index) => Math.round(value + (b[index] - value) * amount).toString(16).padStart(2, "0"))
    .join("")}`;
}

interface Tint {
  base: string;
  light: string;
  shade: string;
  rgba: (alpha: number) => string;
}

// A sphere in one colour: a lit side (towards the upper left), the colour, a side in shadow.
const TINTS = new Map<string, Tint>();
function tintOf(color: string): Tint {
  let tint = TINTS.get(color);
  if (!tint) {
    const [r, g, b] = rgbOf(color);
    tint = {
      base: color,
      light: blend(color, "#ffffff", 0.6),
      shade: blend(color, SPACE, 0.5),
      rgba: (alpha: number) => `rgba(${r}, ${g}, ${b}, ${alpha})`,
    };
    TINTS.set(color, tint);
  }
  return tint;
}

/** An entry's radius in graph units: the Sun, then planets and moons by how many relations they have. */
function nodeRadius(node: GraphNode, orbits: Orbits): number {
  const kind = orbits.byId.get(node.id)?.kind ?? "moon";
  const links = Math.sqrt(orbits.degree.get(node.id) ?? 0);
  if (kind === "sun") return isMe(node) ? SUN_RADIUS : CENTRE_RADIUS;
  if (kind === "planet") return 7 + Math.min(4, links * 1.2);
  return 3.8 + Math.min(2.6, links * 0.9);
}

const [SUN_R, SUN_G, SUN_B] = rgbOf(SUN_COLORS.body);
const [RIM_R, RIM_G, RIM_B] = rgbOf(SUN_COLORS.rim);

/** The Sun: a golden-amber sphere (#FFF3B0 core, #FFB703, an #E85D04 limb) in an amber corona. */
function drawSun(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  const corona = ctx.createRadialGradient(x, y, r * 0.6, x, y, r * 3.4);
  corona.addColorStop(0, `rgba(${SUN_R}, ${SUN_G}, ${SUN_B}, 0.42)`);
  corona.addColorStop(0.35, `rgba(${RIM_R}, ${RIM_G}, ${RIM_B}, 0.16)`);
  corona.addColorStop(1, `rgba(${RIM_R}, ${RIM_G}, ${RIM_B}, 0)`);
  ctx.fillStyle = corona;
  ctx.beginPath();
  ctx.arc(x, y, r * 3.4, 0, 2 * Math.PI);
  ctx.fill();
  const body = ctx.createRadialGradient(x - r * 0.28, y - r * 0.32, r * 0.1, x, y, r);
  body.addColorStop(0, SUN_COLORS.core);
  body.addColorStop(0.62, SUN_COLORS.body);
  body.addColorStop(1, SUN_COLORS.rim);
  ctx.fillStyle = body;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fill();
}

function drawOrb(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, tint: Tint, glow: number, scale: number) {
  if (glow > 0) {
    const reach = r * 2.7;
    const halo = ctx.createRadialGradient(x, y, r * 0.5, x, y, reach);
    halo.addColorStop(0, tint.rgba(glow));
    halo.addColorStop(0.45, tint.rgba(glow * 0.32));
    halo.addColorStop(1, tint.rgba(0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(x, y, reach, 0, 2 * Math.PI);
    ctx.fill();
  }
  // A hairline of space round the body keeps two orbs that touch apart.
  ctx.fillStyle = SPACE;
  ctx.beginPath();
  ctx.arc(x, y, r + 1.2 / scale, 0, 2 * Math.PI);
  ctx.fill();
  const body = ctx.createRadialGradient(x - r * 0.38, y - r * 0.42, r * 0.08, x, y, r);
  body.addColorStop(0, tint.light);
  body.addColorStop(0.55, tint.base);
  body.addColorStop(1, tint.shade);
  ctx.fillStyle = body;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fill();
}

/** Done: a faint emerald ring round the orb and a check badge on its top-right edge. */
function drawDone(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, scale: number) {
  ctx.beginPath();
  ctx.arc(x, y, r + 2.4 / scale, 0, 2 * Math.PI);
  ctx.lineWidth = 1.1 / scale;
  ctx.strokeStyle = DONE_RING;
  ctx.stroke();
  // At least 4.5px across on screen, never larger than most of the orb.
  const b = Math.min(Math.max(r * 0.5, 4.5 / scale), r * 0.8);
  const bx = x + r * 0.72;
  const by = y - r * 0.72;
  ctx.fillStyle = SPACE;
  ctx.beginPath();
  ctx.arc(bx, by, b + 1 / scale, 0, 2 * Math.PI);
  ctx.fill();
  ctx.fillStyle = DONE;
  ctx.beginPath();
  ctx.arc(bx, by, b, 0, 2 * Math.PI);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(bx - b * 0.45, by + b * 0.02);
  ctx.lineTo(bx - b * 0.12, by + b * 0.36);
  ctx.lineTo(bx + b * 0.48, by - b * 0.34);
  ctx.lineWidth = Math.max(b * 0.28, 0.9 / scale);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.strokeStyle = DONE_INK;
  ctx.stroke();
}

/** Archived: a muted dashed ring round the (already greyed and faded) orb. */
function drawArchived(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, scale: number) {
  ctx.beginPath();
  ctx.arc(x, y, r + 3 / scale, 0, 2 * Math.PI);
  ctx.setLineDash([2.5 / scale, 2.5 / scale]);
  ctx.lineWidth = 1 / scale;
  ctx.strokeStyle = ARCHIVED_RING;
  ctx.stroke();
  ctx.setLineDash([]);
}

/**
 * Keeps the entries on one ring apart along it - a collision in one dimension, round each
 * circle, that keeps the order they were seeded in: planets at least 88% of an even share of
 * the inner orbit (so every planet has room for its name), moons at least a moon's width (so
 * they still gather by the planet they hang off). forceRadial holds the rings; this spaces them.
 */
function forceSpread(orbitsNow: () => Orbits, strength = 0.9) {
  let nodes: FGNode[] = [];
  const force = (alpha: number) => {
    const orbits = orbitsNow();
    const rings = new Map<number, { node: FGNode; angle: number }[]>();
    for (const node of nodes) {
      const ring = orbits.byId.get((node as GraphNode).id)?.ring ?? 0;
      if (ring === 0 || node.x === undefined || node.y === undefined) continue;
      rings.set(ring, [...(rings.get(ring) ?? []), { node, angle: Math.atan2(node.y, node.x) }]);
    }
    for (const [ring, members] of rings) {
      if (members.length < 2) continue;
      const radius = orbits.radii[ring] || 1;
      const even = (2 * Math.PI) / members.length;
      const gap = ring === 1 && ring !== orbits.driftRing ? even * 0.88 : Math.min(even, MOON_GAP / radius);
      members.sort((a, b) => a.angle - b.angle);
      members.forEach((a, index) => {
        const b = members[(index + 1) % members.length];
        const apart = b.angle - a.angle + (index === members.length - 1 ? 2 * Math.PI : 0);
        if (apart >= gap) return;
        const push = ((gap - apart) / 2) * strength * alpha * radius;
        // a backwards round the circle, b forwards (the tangent towards a growing angle is (-sin, cos)).
        a.node.vx = (a.node.vx ?? 0) + Math.sin(a.angle) * push;
        a.node.vy = (a.node.vy ?? 0) - Math.cos(a.angle) * push;
        b.node.vx = (b.node.vx ?? 0) - Math.sin(b.angle) * push;
        b.node.vy = (b.node.vy ?? 0) + Math.cos(b.angle) * push;
      });
    }
  };
  force.initialize = (all: FGNode[]) => {
    nodes = all;
  };
  return force;
}

/**
 * Where a name goes when it hangs off its orb towards `angle` (0 = right, PI/2 = down): the
 * anchor, the text alignment that keeps it clear of the orb, and its box (centre, half sizes).
 */
function labelSpot(x: number, y: number, angle: number, gap: number, width: number, height: number) {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  const ax = x + cos * gap;
  const ay = y + sin * gap;
  const align: CanvasTextAlign = cos > 0.38 ? "left" : cos < -0.38 ? "right" : "center";
  const baseline: CanvasTextBaseline = sin > 0.38 ? "top" : sin < -0.38 ? "bottom" : "middle";
  const cx = align === "left" ? ax + width / 2 : align === "right" ? ax - width / 2 : ax;
  const cy = baseline === "top" ? ay + height / 2 : baseline === "bottom" ? ay - height / 2 : ay;
  const box: Box = [cx, cy, width / 2, height / 2];
  return { x: ax, y: ay, align, baseline, box };
}

/** A relation's path as force-graph draws it: straight, or bent by `curvature` x its length. */
function trace(ctx: CanvasRenderingContext2D, s: GraphNode, t: GraphNode, curvature: number) {
  const sx = s.x ?? 0;
  const sy = s.y ?? 0;
  const tx = t.x ?? 0;
  const ty = t.y ?? 0;
  ctx.moveTo(sx, sy);
  const length = Math.hypot(tx - sx, ty - sy);
  if (!curvature || !length) {
    ctx.lineTo(tx, ty);
    return;
  }
  const angle = Math.atan2(ty - sy, tx - sx);
  const d = length * curvature;
  ctx.quadraticCurveTo((sx + tx) / 2 + d * Math.cos(angle - Math.PI / 2), (sy + ty) / 2 + d * Math.sin(angle - Math.PI / 2), tx, ty);
}

// A still sky: the same stars for the same size (a fixed seed), redrawn only when the canvas resizes.
function seeded(seed: number) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function paintSky(canvas: HTMLCanvasElement, width: number, height: number) {
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  // Two or three faint nebulae for depth, never brighter than the faintest orbit ring.
  const span = Math.max(width, height);
  for (const [fx, fy, reach, color] of [
    [0.16, 0.2, 0.55, "rgba(56, 89, 190, 0.10)"],
    [0.86, 0.8, 0.6, "rgba(126, 64, 170, 0.07)"],
    [0.62, 0.08, 0.32, "rgba(20, 130, 150, 0.05)"],
  ] as const) {
    const glow = ctx.createRadialGradient(fx * width, fy * height, 0, fx * width, fy * height, reach * span);
    glow.addColorStop(0, color);
    glow.addColorStop(1, "rgba(6, 9, 19, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, width, height);
  }
  const random = seeded(20261119);
  const count = Math.round((width * height) / 3200);
  const tints = ["255, 255, 255", "205, 222, 255", "255, 236, 214"];
  for (let index = 0; index < count; index++) {
    const x = random() * width;
    const y = random() * height;
    const bright = random() > 0.93;
    const radius = bright ? 0.8 + random() * 0.6 : 0.3 + random() * 0.5;
    const alpha = bright ? 0.5 + random() * 0.3 : 0.14 + random() * 0.36;
    ctx.fillStyle = `rgba(${tints[Math.floor(random() * tints.length)]}, ${alpha.toFixed(2)})`;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fill();
  }
}

export default function SolarCanvas({
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
}: SolarCanvasProps) {
  const box = useRef<HTMLDivElement | null>(null);
  const sky = useRef<HTMLCanvasElement | null>(null);
  const aura = useRef<HTMLDivElement | null>(null);
  const auraBreath = useRef<HTMLDivElement | null>(null);
  const tooltip = useRef<HTMLDivElement | null>(null);
  const fg = useRef<ForceGraphMethods<FGNode, FGLink> | undefined>(undefined);
  const fitted = useRef(false);
  const family = useRef<string | null>(null);
  // The zoom of the frame being drawn: dashes are screen pixels at any zoom.
  const scaleRef = useRef(1);
  // The forces read the latest orbits when the layout restarts (after a delete).
  const orbitsRef = useRef(orbits);
  // The labels drawn this frame, so two never overlap.
  const labelBoxes = useRef<Box[]>([]);
  const nameBoxes = useRef<Box[]>([]);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [hovered, setHovered] = useState<GraphNode | null>(null);
  const mounted = size !== null;

  useEffect(() => {
    orbitsRef.current = orbits;
  }, [orbits]);

  // The orbits: forceRadial holds each entry on its ring, the charge, a collision force and the
  // spread along each ring space them, and weak links pull each moon towards the planet it hangs
  // off. Set before the library's first update (it runs a moment after mount), so the warm-up
  // ticks use them.
  useEffect(() => {
    const api = fg.current;
    if (!api) return;
    const ringOf = (node: FGNode | string | number | undefined) =>
      typeof node === "object" && node ? (orbitsRef.current.byId.get((node as GraphNode).id)?.ring ?? 0) : 0;
    const radiusAt = (ring: number) => orbitsRef.current.radii[ring] ?? 0;
    api.d3Force("center", null);
    api.d3Force("charge")?.strength(-110).distanceMax(280);
    api
      .d3Force("link")
      ?.distance((link: FGLink) => {
        const a = ringOf(link.source);
        const b = ringOf(link.target);
        return a === b ? 70 : Math.max(60, Math.abs(radiusAt(a) - radiusAt(b)));
      })
      .strength(0.1);
    api.d3Force(
      "orbit",
      forceRadial<SimNode>((node) => radiusAt(ringOf(node)), 0, 0).strength((node) => (ringOf(node) === 0 ? 0 : 0.85)),
    );
    api.d3Force(
      "collide",
      forceCollide<SimNode>((node) => nodeRadius(node as GraphNode, orbitsRef.current) + 6).strength(0.8),
    );
    api.d3Force("spread", forceSpread(() => orbitsRef.current));
  }, [mounted]);

  // The canvas fills its box; the library needs the size in numbers. The sky is redrawn to match.
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      setSize((current) => (current && current.width === width && current.height === height ? current : { width, height }));
      if (sky.current) paintSky(sky.current, width, height);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // The Sun's breath: scale and opacity only, on the compositor - the canvas is not redrawn for it.
  useEffect(() => {
    const element = auraBreath.current;
    if (!element || typeof element.animate !== "function") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const breath = element.animate(
      [
        { transform: "scale(0.93)", opacity: 0.7 },
        { transform: "scale(1.07)", opacity: 1 },
      ],
      { duration: 5200, direction: "alternate", iterations: Infinity, easing: "ease-in-out" },
    );
    return () => breath.cancel();
  }, []);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);

  // The bend that keeps two relations between the same pair apart.
  const curvature = useMemo(() => {
    const pairs = new Map<string, GraphLink[]>();
    for (const link of graph.links) {
      const s = endId(link.source);
      const t = endId(link.target);
      const pair = s < t ? `${s}|${t}` : `${t}|${s}`;
      pairs.set(pair, [...(pairs.get(pair) ?? []), link]);
    }
    const bends = new Map<string, number>();
    for (const links of pairs.values()) {
      if (links.length < 2) continue;
      links.forEach((link, index) => {
        // 0.25, -0.25, 0.5, -0.5... measured against one direction of the pair.
        const step = Math.ceil((index + 1) / 2) * 0.25 * (index % 2 ? -1 : 1);
        bends.set(link.id, endId(link.source) < endId(link.target) ? step : -step);
      });
    }
    return bends;
  }, [graph]);

  const radiusOf = useCallback((node: GraphNode) => nodeRadius(node, orbits), [orbits]);
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

  // Focus: the entry pointed at, else the open one. It lights up with its neighbours; the rest dim.
  const focusId = hovered?.id ?? selectedId;
  const lit = useMemo(
    () => (focusId ? new Set([focusId, ...(orbits.neighbours.get(focusId) ?? [])]) : null),
    [focusId, orbits],
  );
  const touches = useCallback(
    (link: GraphLink) => focusId !== null && (endId(link.source) === focusId || endId(link.target) === focusId),
    [focusId],
  );
  const searchDims = useCallback(
    (link: GraphLink) => matchIds !== null && (!matchIds.has(endId(link.source)) || !matchIds.has(endId(link.target))),
    [matchIds],
  );
  /** Dimmed: outside the lit-up entry's neighbourhood, or not matched by the search. */
  const dimmed = useCallback(
    (id: string) => (lit !== null && !lit.has(id)) || (matchIds !== null && !matchIds.has(id)),
    [lit, matchIds],
  );
  /** How strongly an entry is drawn: dimmed outside the focus or the search; an archived one recedes to 35%. */
  const alphaOf = useCallback(
    (id: string) => {
      if (lit !== null && !lit.has(id)) return 0.1;
      if (matchIds !== null && !matchIds.has(id)) return 0.16;
      if (id !== orbits.centerId && looks.status.get(id) === "archived") return lit !== null ? 0.6 : ARCHIVED_ALPHA;
      return 1;
    },
    [lit, matchIds, looks, orbits.centerId],
  );

  // ---- the canvas API the page drives --------------------------------------------------------

  const fit = useCallback(
    (durationMs = 450) => {
      const api = fg.current;
      if (!api || !size) return;
      const bbox = api.getGraphBbox((node) => visible(node as GraphNode));
      if (!bbox) return;
      const clear = insets();
      const left = clear.left + FIT_PADDING;
      const right = Math.max(left + 120, size.width - clear.right - FIT_PADDING);
      const top = clear.top + FIT_PADDING;
      const bottom = Math.max(top + 120, size.height - clear.bottom - FIT_PADDING);
      const k = Math.min(
        MAX_FIT_ZOOM,
        Math.max(
          MIN_ZOOM,
          Math.min((right - left) / Math.max(1, bbox.x[1] - bbox.x[0]), (bottom - top) / Math.max(1, bbox.y[1] - bbox.y[0])),
        ),
      );
      const cx = (bbox.x[0] + bbox.x[1]) / 2;
      const cy = (bbox.y[0] + bbox.y[1]) / 2;
      // centerAt puts a graph point at the canvas's middle; shift it so the frame's middle lands
      // in the middle of the clear part instead.
      const ms = durationMs * motion();
      api.centerAt(cx + (size.width / 2 - (left + right) / 2) / k, cy + (size.height / 2 - (top + bottom) / 2) / k, ms);
      api.zoom(k, ms);
    },
    [insets, size, visible],
  );

  const focus = useCallback(
    (id: string) => {
      const api = fg.current;
      const node = nodeById.get(id);
      if (!api || !size || !node || node.x === undefined || node.y === undefined) return;
      const clear = insets();
      const k = Math.max(api.zoom(), FOCUS_ZOOM);
      const middleX = (clear.left + size.width - clear.right) / 2;
      const middleY = (clear.top + size.height - clear.bottom) / 2;
      const ms = 550 * motion();
      api.centerAt(node.x + (size.width / 2 - middleX) / k, node.y + (size.height / 2 - middleY) / k, ms);
      api.zoom(k, ms);
    },
    [insets, nodeById, size],
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
    fit(0);
  }, [fit]);

  // ---- painting ----------------------------------------------------------------------------------

  const paintNode = useCallback(
    (raw: FGNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const node = raw as GraphNode;
      if (node.x === undefined || node.y === undefined) return;
      const kind = orbits.byId.get(node.id)?.kind ?? "moon";
      const r = radiusOf(node);
      const selected = node.id === selectedId;
      const hover = hovered?.id === node.id;
      const alpha = alphaOf(node.id);
      const inFocus = lit !== null && lit.has(node.id);
      ctx.globalAlpha = alpha;

      if (kind === "sun") {
        // The person's own Sun; with no "me", the most connected entry holds the centre, smaller.
        drawSun(ctx, node.x, node.y, r);
      } else {
        // Status on the orb itself: Active glows in full, Done calmer with its badge, Archived
        // greyed without a glow inside a dashed ring. Nothing dimmed glows.
        const state = looks.status.get(node.id) ?? "active";
        const color = colorOf(node.id, orbits, looks);
        const strong = selected || hover || inFocus;
        const glow =
          state === "archived" || dimmed(node.id)
            ? 0
            : (strong ? 0.58 : kind === "moon" ? 0.24 : 0.36) * (state === "completed" ? 0.45 : 1);
        drawOrb(ctx, node.x, node.y, r, tintOf(state === "archived" ? blend(color, ARCHIVED_GREY, 0.62) : color), glow, scale);
        if (state === "completed") drawDone(ctx, node.x, node.y, r, scale);
        else if (state === "archived") drawArchived(ctx, node.x, node.y, r, scale);
      }
      if (selected || hover) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + (selected ? 4 : 3.5) / scale, 0, 2 * Math.PI);
        ctx.lineWidth = (selected ? 1.75 : 1) / scale;
        ctx.strokeStyle = selected ? "#ffffff" : "rgba(255, 255, 255, 0.6)";
        ctx.stroke();
      }

      // The name, a fixed size on screen at any zoom. Always: the open, pointed-at, matched and
      // lit-up entries, and - with floating labels on - the Sun and every planet; the moons once
      // zoomed in, where they do not cover a name already drawn (zooming in makes room). Each
      // name sits outward from the Sun, like a label on a map (a planet at the top is named above
      // it, one on the right to its right), else under or over its orb if that spot is taken.
      const important = selected || hover || inFocus || (matchIds !== null && matchIds.has(node.id));
      const wanted = important || (labels && (kind !== "moon" || scale >= MOON_LABEL_ZOOM));
      if (!wanted) {
        ctx.globalAlpha = 1;
        return;
      }
      family.current ??= canvasFont(ctx);
      const px = kind === "sun" ? 13 : kind === "planet" ? 12 : 11;
      const size = px / scale;
      ctx.font = `${kind === "sun" || selected ? 600 : 500} ${size}px ${family.current}`;
      // Zoomed out, a long planet name is cut shorter so its neighbours keep room for theirs.
      const text = clip(displayName(node), kind === "moon" ? 22 : scale < 1 ? 18 : 26);
      const gap = (kind === "sun" ? r * 1.25 : r) + 4 / scale;
      const width = ctx.measureText(text).width + 4 / scale;
      const height = size * 1.25;
      const outward = kind === "sun" ? Math.PI / 2 : Math.atan2(node.y, node.x);
      const spots = [outward, Math.PI / 2, -Math.PI / 2].map((angle) => labelSpot(node.x ?? 0, node.y ?? 0, angle, gap, width, height));
      const boxes = nameBoxes.current;
      const free = ([x, y, w, h]: Box) => !boxes.some(([bx, by, bw, bh]) => Math.abs(bx - x) < bw + w && Math.abs(by - y) < bh + h);
      let spot = spots.find((candidate) => free(candidate.box));
      if (!spot) {
        if (!important && kind === "moon") {
          ctx.globalAlpha = 1;
          return;
        }
        spot = spots[0];
      }
      boxes.push(spot.box);
      ctx.textAlign = spot.align;
      ctx.textBaseline = spot.baseline;
      ctx.lineJoin = "round";
      ctx.lineWidth = 3.5 / scale;
      ctx.strokeStyle = LABEL_HALO;
      ctx.strokeText(text, spot.x, spot.y);
      ctx.fillStyle = kind === "sun" ? TEXT_SUN : kind === "planet" || important ? TEXT_PLANET : TEXT_MOON;
      ctx.fillText(text, spot.x, spot.y);
      ctx.globalAlpha = 1;
    },
    // revision: an entry's words changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [orbits, looks, radiusOf, selectedId, hovered, alphaOf, dimmed, lit, matchIds, labels, revision],
  );

  const paintPointerArea = useCallback(
    (raw: FGNode, color: string, ctx: CanvasRenderingContext2D, scale: number) => {
      const node = raw as GraphNode;
      if (node.x === undefined || node.y === undefined) return;
      // Bigger than the orb: easy to hit with a finger.
      ctx.beginPath();
      ctx.arc(node.x, node.y, radiusOf(node) + 7 / scale, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [radiusOf],
  );

  const linkColor = useCallback(
    (raw: FGLink) => {
      const link = raw as GraphLink;
      const holds = link.status === "active";
      if (focusId !== null) return touches(link) ? (holds ? LINK_LIT : LINK_LIT_ENDED) : LINK_DARK;
      if (searchDims(link)) return LINK_DARK;
      return LINK[link.status] ?? LINK.active;
    },
    // revision: a status changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [focusId, touches, searchDims, revision],
  );

  const linkDash = useCallback(
    (raw: FGLink) => {
      const dash = RELATION_DASH[(raw as GraphLink).status];
      return dash ? dash.map((part) => part / scaleRef.current) : null;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [revision],
  );

  const linkWidth = useCallback(
    (raw: FGLink) => {
      const link = raw as GraphLink;
      const base = link.status === "active" ? 1 + Math.min(1.5, Math.log2(Math.max(1, link.weight)) * 0.6) : 0.9;
      return touches(link) ? base + 0.6 : base;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [touches, revision],
  );

  // Before the relations: the orbit rings, then a soft glow under every fact that holds.
  const paintBackdrop = useCallback(
    (ctx: CanvasRenderingContext2D, scale: number) => {
      scaleRef.current = scale;
      labelBoxes.current = [];
      nameBoxes.current = [];
      ctx.save();
      ctx.lineWidth = RING_WIDTH / scale;
      orbits.radii.forEach((radius, ring) => {
        if (ring === 0) return;
        const drifting = ring === orbits.driftRing;
        ctx.beginPath();
        ctx.arc(0, 0, radius, 0, 2 * Math.PI);
        ctx.strokeStyle = drifting ? RING_DRIFT : RING;
        ctx.setLineDash(drifting ? [2 / scale, 6 / scale] : []);
        ctx.stroke();
      });
      ctx.setLineDash([]);
      for (const link of graph.links) {
        if (link.status !== "active" || !linkVisible(link)) continue;
        const s = link.source;
        const t = link.target;
        if (typeof s !== "object" || typeof t !== "object" || s.x === undefined || t.x === undefined) continue;
        const near = touches(link);
        if ((focusId !== null && !near) || (focusId === null && searchDims(link))) continue;
        ctx.beginPath();
        trace(ctx, s, t, curvature.get(link.id) ?? 0);
        ctx.strokeStyle = near ? GLOW_NEAR : GLOW_FAR;
        ctx.lineWidth = (near ? 7 : 4) / scale;
        ctx.stroke();
      }
      ctx.restore();
    },
    // revision: a status changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [orbits, graph, linkVisible, touches, searchDims, focusId, curvature, revision],
  );

  // After every frame: the Sun's aura follows the Sun on screen (the canvas only redraws on change).
  const placeAura = useCallback(
    (_ctx: CanvasRenderingContext2D, scale: number) => {
      const layer = aura.current;
      const api = fg.current;
      if (!layer || !api) return;
      const center = orbits.centerId ? nodeById.get(orbits.centerId) : undefined;
      if (!center || !orbits.centerIsMe || center.x === undefined || center.y === undefined) {
        layer.style.opacity = "0";
        return;
      }
      const at = api.graph2ScreenCoords(center.x, center.y);
      const across = Math.max(48, SUN_RADIUS * scale * AURA_REACH);
      layer.style.opacity = String(alphaOf(center.id) < 1 ? 0.25 : 1);
      layer.style.transform = `translate(${at.x}px, ${at.y}px) translate(-50%, -50%) scale(${across / AURA_SIZE})`;
    },
    [orbits, nodeById, alphaOf],
  );

  const paintLinkLabel = useCallback(
    (raw: FGLink, ctx: CanvasRenderingContext2D, scale: number) => {
      const link = raw as GraphLink;
      const s = typeof link.source === "object" ? link.source : null;
      const t = typeof link.target === "object" ? link.target : null;
      if (!s || !t || s.x === undefined || s.y === undefined || t.x === undefined || t.y === undefined) return;
      const near = touches(link);
      if (!near && (focusId !== null || scale < LINK_LABEL_ZOOM || searchDims(link))) return;

      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const length = Math.hypot(dx, dy);
      family.current ??= canvasFont(ctx);
      const size = 10 / scale;
      ctx.font = `500 ${size}px ${family.current}`;
      const text = clip(relationLabel(link.relation_type), 24);
      const textWidth = ctx.measureText(text).width;
      // No room between the two orbs: leave it to the panel.
      if (length - radiusOf(s) - radiusOf(t) < textWidth + 6 / scale) return;

      // The middle of the (possibly bent) line: force-graph bends it by curvature x length.
      const bend = curvature.get(link.id) ?? 0;
      const angle = Math.atan2(dy, dx);
      const offset = (length * bend) / 2;
      const x = (s.x + t.x) / 2 + offset * Math.cos(angle - Math.PI / 2);
      const y = (s.y + t.y) / 2 + offset * Math.sin(angle - Math.PI / 2);

      // Where the turned label lands, as a box; one that would cover another is left out, except
      // on the lit entry's relations, which always say what they are.
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
      ctx.strokeStyle = LABEL_HALO;
      ctx.strokeText(text, 0, 0);
      ctx.fillStyle = near ? TEXT_PLANET : TEXT_LINK;
      ctx.fillText(text, 0, 0);
      ctx.restore();
    },
    [touches, focusId, searchDims, radiusOf, curvature],
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

  const hoveredInfo = hovered ? orbits.byId.get(hovered.id) : undefined;
  const hoveredCount = hovered ? (orbits.degree.get(hovered.id) ?? 0) : 0;
  const hoveredStatus = hovered && hoveredInfo?.kind !== "sun" ? (looks.status.get(hovered.id) ?? "active") : "active";
  // A moon names the planet whose family it is in.
  const hoveredFamily =
    hovered && hoveredInfo?.kind === "moon" ? (nodeById.get(looks.family.get(hovered.id) ?? "") ?? null) : null;

  return (
    <div ref={box} className="absolute inset-0 overflow-hidden" onPointerMove={onPointerMove} onPointerLeave={() => setHovered(null)}>
      <canvas ref={sky} aria-hidden="true" className="pointer-events-none absolute inset-0 size-full" />
      <div
        ref={aura}
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 opacity-0"
        style={{ width: AURA_SIZE, height: AURA_SIZE }}
      >
        <div
          ref={auraBreath}
          className="size-full rounded-full"
          style={{
            background:
              "radial-gradient(circle, rgba(255, 183, 3, 0.36) 0%, rgba(232, 93, 4, 0.16) 30%, rgba(232, 93, 4, 0.05) 52%, rgba(232, 93, 4, 0) 70%)",
          }}
        />
      </div>

      {size && (
        <ForceGraph2D<GraphNode, GraphLink>
          ref={fg}
          graphData={graph}
          width={size.width}
          height={size.height}
          backgroundColor="rgba(0, 0, 0, 0)"
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
          linkDirectionalArrowLength={4.5}
          linkDirectionalArrowRelPos={1}
          linkDirectionalArrowColor={linkColor}
          linkCanvasObjectMode={() => "after"}
          linkCanvasObject={paintLinkLabel}
          onRenderFramePre={paintBackdrop}
          onRenderFramePost={placeAura}
          // Settled before the first frame: the orbits are still from the start.
          warmupTicks={220}
          cooldownTicks={50}
          enableNodeDrag={false}
          minZoom={MIN_ZOOM}
          maxZoom={MAX_ZOOM}
          onEngineTick={firstFrame}
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
