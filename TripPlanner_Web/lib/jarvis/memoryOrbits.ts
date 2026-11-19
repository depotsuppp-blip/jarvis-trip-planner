/**
 * The Memory page's Solar System: where each entry sits, worked out from the graph alone.
 * Pure - no React, no canvas - so the page and the canvas read the same answer.
 *
 *   the Sun      the person's own entry ("me") at the centre (with no "me", the most
 *                connected entry takes the centre, drawn as a planet)
 *   planets      entries one relation away from the centre: the inner orbit
 *   moons        two relations away or more: the outer orbits, one ring per hop (from the
 *                fourth hop on they share the last), each near the planet it hangs off
 *   outer orbit  entries no chain of relations links to the centre: one faint ring outside
 *                the rest (they are moons too)
 *
 * A relation counts whatever its status: a fact that ended is still history about you.
 * The rings grow with what is on them, so a crowded orbit never stacks its names.
 */

import { isMe, type MemoryLink, type MemoryNode } from "./memory";

export type OrbitKind = "sun" | "planet" | "moon";

export interface OrbitInfo {
  kind: OrbitKind;
  /** Relations between this entry and the centre: 0 the centre itself; null when none link them. */
  hops: number | null;
  /** The ring it sits on: 0 is the centre. */
  ring: number;
}

export interface Orbits {
  /** The entry at the centre, or null for an empty graph. */
  centerId: string | null;
  /** Whether the centre is the person's own entry - the Sun - rather than the most connected one. */
  centerIsMe: boolean;
  byId: Map<string, OrbitInfo>;
  /** Each ring's radius in graph units; radii[0] is 0 (the centre). */
  radii: number[];
  /** The ring of entries not linked to the centre, or -1 when every entry is linked. */
  driftRing: number;
  /** How many relations each entry has. */
  degree: Map<string, number>;
  /** Each entry's neighbours (either direction). */
  neighbours: Map<string, Set<string>>;
  counts: { planets: number; moons: number; drifting: number };
}

/** A relation's end as the graph library leaves it: an id, or the node object itself. */
type End = string | { id: string };
interface LinkEnds {
  source: End;
  target: End;
}

export const endId = (end: End | undefined | null): string =>
  typeof end === "object" && end !== null ? end.id : String(end ?? "");

// Hops past this share the last ring.
const LAST_HOP_RING = 4;
const FIRST_RADIUS = 150;
const RING_GAP = 115;
// Room along a ring for one entry (graph units of arc): planets carry names at every zoom.
const PLANET_ARC = 120;
const MOON_ARC = 48;

export function computeOrbits(nodes: readonly MemoryNode[], links: readonly LinkEnds[]): Orbits {
  const ids = new Set(nodes.map((node) => node.id));
  const neighbours = new Map<string, Set<string>>(nodes.map((node) => [node.id, new Set<string>()]));
  const degree = new Map<string, number>(nodes.map((node) => [node.id, 0]));
  for (const link of links) {
    const s = endId(link.source);
    const t = endId(link.target);
    if (!ids.has(s) || !ids.has(t)) continue;
    degree.set(s, (degree.get(s) ?? 0) + 1);
    degree.set(t, (degree.get(t) ?? 0) + 1);
    if (s === t) continue;
    neighbours.get(s)?.add(t);
    neighbours.get(t)?.add(s);
  }

  const me = nodes.find((node) => isMe(node));
  let center: MemoryNode | undefined = me;
  if (!center) {
    for (const node of nodes) {
      const d = degree.get(node.id) ?? 0;
      const best = center ? (degree.get(center.id) ?? 0) : -1;
      if (d > best || (d === best && center && node.name.localeCompare(center.name) < 0)) center = node;
    }
  }

  // Breadth first from the centre: hops = the fewest relations between an entry and it.
  const hops = new Map<string, number>();
  if (center) {
    hops.set(center.id, 0);
    let frontier = [center.id];
    for (let hop = 1; frontier.length; hop++) {
      const next: string[] = [];
      for (const id of frontier) {
        for (const other of neighbours.get(id) ?? []) {
          if (hops.has(other)) continue;
          hops.set(other, hop);
          next.push(other);
        }
      }
      frontier = next;
    }
  }

  let lastRing = 0;
  for (const hop of hops.values()) lastRing = Math.max(lastRing, Math.min(hop, LAST_HOP_RING));
  const anyDrifting = nodes.some((node) => !hops.has(node.id));
  const driftRing = anyDrifting ? lastRing + 1 : -1;

  const byId = new Map<string, OrbitInfo>();
  const perRing: number[] = [];
  const counts = { planets: 0, moons: 0, drifting: 0 };
  for (const node of nodes) {
    const hop = hops.get(node.id);
    const ring = hop === undefined ? driftRing : Math.min(hop, LAST_HOP_RING);
    const kind: OrbitKind = hop === 0 ? "sun" : hop === 1 ? "planet" : "moon";
    byId.set(node.id, { kind, hops: hop ?? null, ring });
    perRing[ring] = (perRing[ring] ?? 0) + 1;
    if (hop === undefined) counts.drifting += 1;
    else if (kind === "planet") counts.planets += 1;
    else if (kind === "moon") counts.moons += 1;
  }

  const radii = [0];
  const rings = Math.max(lastRing, driftRing);
  for (let ring = 1; ring <= rings; ring++) {
    const floor = ring === 1 ? FIRST_RADIUS : radii[ring - 1] + RING_GAP;
    const arc = ring === 1 && driftRing !== 1 ? PLANET_ARC : MOON_ARC;
    radii.push(Math.max(floor, ((perRing[ring] ?? 0) * arc) / (2 * Math.PI)));
  }

  return {
    centerId: center?.id ?? null,
    centerIsMe: !!me,
    byId,
    radii,
    driftRing,
    degree,
    neighbours,
    counts,
  };
}

/** An entry as the canvas holds it: the graph library reads x/y and pins fx/fy. */
export interface GraphNode extends MemoryNode {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
}

/** A relation as the canvas holds it: the library swaps each end's id for the node itself. */
export type GraphLink = Omit<MemoryLink, "source" | "target"> & { source: string | GraphNode; target: string | GraphNode };

/** The page's working copy of the graph (the library writes positions into these objects). */
export interface SolarGraph {
  nodes: GraphNode[];
  links: GraphLink[];
}

/**
 * Puts every entry that has no place yet on its ring, before the first layout: planets evenly
 * round the inner orbit (grouped by type, so a constellation of one colour reads as one), each
 * moon at the angle of the planet it hangs off, nudged along its ring so none start on top of
 * another. The centre is pinned at (0, 0). Starting this close to the answer is what lets the
 * layout settle before the first frame - the orbits are still from the moment they appear.
 */
export function seedOrbits(nodes: GraphNode[], orbits: Orbits): void {
  const angle = new Map<string, number>();
  const byRing = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const info = orbits.byId.get(node.id);
    if (!info) continue;
    if (info.ring === 0) {
      node.x = 0;
      node.y = 0;
      node.fx = 0;
      node.fy = 0;
      angle.set(node.id, 0);
      continue;
    }
    byRing.set(info.ring, [...(byRing.get(info.ring) ?? []), node]);
  }

  const rings = [...byRing.keys()].sort((a, b) => a - b);
  for (const ring of rings) {
    const members = byRing.get(ring) ?? [];
    const radius = orbits.radii[ring] ?? 0;
    const drifting = ring === orbits.driftRing;
    // Where each would like to be: a planet by its type and name, a moon by its parent planet.
    const wanted = members.map((node) => {
      if (ring === 1 || drifting) return { node, at: NaN };
      let at = NaN;
      for (const other of orbits.neighbours.get(node.id) ?? []) {
        const parent = orbits.byId.get(other);
        if (parent && parent.ring === ring - 1 && angle.has(other)) {
          at = angle.get(other) ?? NaN;
          break;
        }
      }
      return { node, at };
    });
    const even = (list: typeof wanted) =>
      list.forEach((entry, index) => angle.set(entry.node.id, -Math.PI / 2 + (index * 2 * Math.PI) / list.length));

    if (ring === 1 || drifting) {
      even([...wanted].sort((a, b) => a.node.type.localeCompare(b.node.type) || a.node.name.localeCompare(b.node.name)));
    } else {
      const sorted = [...wanted].sort((a, b) => (a.at || 0) - (b.at || 0) || a.node.name.localeCompare(b.node.name));
      const step = MOON_ARC / Math.max(radius, 1);
      if (step * sorted.length >= 2 * Math.PI) {
        even(sorted);
      } else {
        // Each at its parent's angle, then pushed along just enough to clear the one before it.
        let previous = -Infinity;
        const placed = sorted.map((entry) => {
          const at = Math.max(Number.isNaN(entry.at) ? previous + step : entry.at, previous + step);
          previous = at;
          return at;
        });
        sorted.forEach((entry, index) => angle.set(entry.node.id, placed[index]));
      }
    }

    for (const node of members) {
      if (node.x !== undefined && node.y !== undefined) continue;
      const at = angle.get(node.id) ?? 0;
      node.x = radius * Math.cos(at);
      node.y = radius * Math.sin(at);
    }
  }
}

/**
 * After entries or relations go, the graph that is left: the centre pinned at (0, 0) - with no
 * "me", the most connected entry can change - and every other entry free to find its orbit.
 */
export function settle(nodes: GraphNode[], links: GraphLink[]): SolarGraph {
  const centerId = computeOrbits(nodes, links).centerId;
  for (const node of nodes) {
    if (node.id === centerId) {
      node.fx = 0;
      node.fy = 0;
    } else if (node.fx !== undefined || node.fy !== undefined) {
      delete node.fx;
      delete node.fy;
    }
  }
  return { nodes, links };
}

/** Entries whose every relation has ended (completed or archived): drawn dimmer, as history. */
export function endedEntries(
  links: readonly (LinkEnds & { status: string })[],
): Set<string> {
  const active = new Set<string>();
  const touched = new Set<string>();
  for (const link of links) {
    for (const id of [endId(link.source), endId(link.target)]) {
      touched.add(id);
      if (link.status === "active") active.add(id);
    }
  }
  return new Set([...touched].filter((id) => !active.has(id)));
}

/** How an entry's place reads in words: "Planet · 1 hop from you". */
export function orbitLabel(info: OrbitInfo | undefined, centerIsMe: boolean): string {
  if (!info) return "";
  const from = centerIsMe ? "you" : "the centre";
  if (info.kind === "sun") return centerIsMe ? "The Sun · you" : "Centre · the most connected entry";
  if (info.hops === null) return `Outer orbit · not linked to ${from}`;
  if (info.kind === "planet") return `Planet · 1 hop from ${from}`;
  return `Moon · ${info.hops} hops from ${from}`;
}

/** The short name of an orbit, for lists and the tooltip. */
export function orbitWord(info: OrbitInfo | undefined, centerIsMe: boolean): string {
  if (!info) return "";
  if (info.kind === "sun") return centerIsMe ? "Sun" : "Centre";
  if (info.hops === null) return "Outer orbit";
  return info.kind === "planet" ? "Planet" : "Moon";
}
