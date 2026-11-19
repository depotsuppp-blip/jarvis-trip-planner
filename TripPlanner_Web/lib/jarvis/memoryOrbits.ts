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
 *
 * Colour follows the family, not the type (computeLooks): each planet gets a signature colour
 * round the inner orbit, and its moons inherit it, so a planet and everything hanging off it
 * read as one group. Each entry also gets a status from its main fact - Active, Done or
 * Archived - drawn on the orb itself.
 */

import { isMe, type MemoryLink, type MemoryNode, type RelationStatus } from "./memory";

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
  /** The planets in their order round the inner orbit (by type, then name): it sets their colours. */
  planetOrder: string[];
  counts: { planets: number; moons: number; drifting: number };
}

/** A relation's end as the graph library leaves it: an id, or the node object itself. */
type End = string | { id: string };
interface LinkEnds {
  source: End;
  target: End;
}

/** A relation as far as colours and statuses care. */
interface LinkFacts extends LinkEnds {
  status: RelationStatus;
  weight: number;
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

  const planetOrder = nodes
    .filter((node) => byId.get(node.id)?.kind === "planet")
    .sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
    .map((node) => node.id);

  return {
    centerId: center?.id ?? null,
    centerIsMe: !!me,
    byId,
    radii,
    driftRing,
    degree,
    neighbours,
    planetOrder,
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
 * round the inner orbit in `planetOrder` (the order their colours are dealt in), each
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

    if (ring === 1 && !drifting) {
      const place = new Map(orbits.planetOrder.map((id, index) => [id, index]));
      even([...wanted].sort((a, b) => (place.get(a.node.id) ?? 0) - (place.get(b.node.id) ?? 0)));
    } else if (drifting) {
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

// ---- colour families and statuses -----------------------------------------------------------

/** The Sun: a warm golden-amber glow - its core, its body, the rim of its aura. */
export const SUN_COLORS = { core: "#FFF3B0", body: "#FFB703", rim: "#E85D04" } as const;

/**
 * The planets' signature colours, dealt round the inner orbit in this order: Neon Crimson,
 * Electric Violet, Emerald, Cyber Blue, Neon Magenta, Indigo, Warm Coral, Cyan. The order is
 * the one (of all 5040) that keeps every two neighbours easiest to tell apart, measured like the
 * dataviz validator: worst neighbouring pair 14.7 for colour-blind vision, 26.5 for normal
 * (in the order Crimson, Violet, Blue, Magenta... it was 1.3 - Violet and Indigo are one colour
 * to a colour-blind eye, so they are never dealt side by side). Every one is over 3:1 on the
 * canvas. Colour never works alone: every planet carries its name, and the panel lists each
 * family under its planet.
 */
export const PLANET_COLORS = ["#FF3B6B", "#8B5CF6", "#10B981", "#3B82F6", "#EC4899", "#6366F1", "#FB923C", "#06B6D4"] as const;
// With one planet over a whole round (9, 17...), the last sits between the last colour and the
// first: Violet is the one furthest from both.
const WRAP_COLOR = 1;

/** Entries no chain of relations links to you belong to no family: a quiet slate. */
export const STRAY_COLOR = "#94A3B8";

/** How much lighter a moon is than its planet (towards white). */
export const MOON_LIGHTEN = 0.32;

/** An entry's status as a badge reads: from its main fact. */
export const ENTRY_STATUS_LABEL: Record<RelationStatus, string> = { active: "Active", completed: "Done", archived: "Archived" };

export interface Looks {
  /** The planet each entry belongs to (a planet is its own); null for the centre and the outer orbit. */
  family: Map<string, string | null>;
  /** Each planet's signature colour. */
  planetColor: Map<string, string>;
  /**
   * Each entry's status: for a planet, its relation to the centre ("me built TripPlanner" -
   * completed = Done); for any other entry, its best relation (one that holds wins). The centre,
   * and an entry with no relations, are active.
   */
  status: Map<string, RelationStatus>;
}

const RANK: Record<RelationStatus, number> = { active: 0, completed: 1, archived: 2 };

/**
 * Colours and statuses for the orbits. Depends on the relations' statuses (which change in
 * place), so it is worked out apart from computeOrbits (which depends on the graph's shape only).
 */
export function computeLooks(nodes: readonly MemoryNode[], links: readonly LinkFacts[], orbits: Orbits): Looks {
  const planetColor = new Map<string, string>();
  const total = orbits.planetOrder.length;
  orbits.planetOrder.forEach((id, index) => {
    let slot = index % PLANET_COLORS.length;
    // The last planet meets the first round the circle: never the same colour as it.
    if (total > 1 && index === total - 1 && slot === 0) slot = WRAP_COLOR;
    planetColor.set(id, PLANET_COLORS[slot]);
  });

  // Per entry, its relations (the other end and the status); per pair, whether a fact holds and how often it was told.
  const facts = new Map<string, { other: string; status: RelationStatus }[]>();
  const pairs = new Map<string, { holds: boolean; weight: number }>();
  for (const link of links) {
    const s = endId(link.source);
    const t = endId(link.target);
    facts.set(s, [...(facts.get(s) ?? []), { other: t, status: link.status }]);
    facts.set(t, [...(facts.get(t) ?? []), { other: s, status: link.status }]);
    const key = s < t ? `${s}|${t}` : `${t}|${s}`;
    const pair = pairs.get(key) ?? { holds: false, weight: 0 };
    pairs.set(key, { holds: pair.holds || link.status === "active", weight: pair.weight + (link.weight || 1) });
  }
  const pairOf = (a: string, b: string) => pairs.get(a < b ? `${a}|${b}` : `${b}|${a}`) ?? { holds: false, weight: 0 };

  const status = new Map<string, RelationStatus>();
  for (const node of nodes) {
    const info = orbits.byId.get(node.id);
    let own = facts.get(node.id) ?? [];
    if (info?.kind === "sun") own = [];
    else if (info?.kind === "planet") own = own.filter((fact) => fact.other === orbits.centerId);
    const best = own.reduce<RelationStatus | null>((found, fact) => (found === null || RANK[fact.status] < RANK[found] ? fact.status : found), null);
    status.set(node.id, best ?? "active");
  }

  // A moon joins the family of the entry it hangs off, one hop nearer the centre; with a choice,
  // the one whose fact with it holds, then an active one, then the most told, then the first planet.
  const family = new Map<string, string | null>();
  const place = new Map(orbits.planetOrder.map((id, index) => [id, index]));
  const byHops = [...nodes].sort((a, b) => (orbits.byId.get(a.id)?.hops ?? Infinity) - (orbits.byId.get(b.id)?.hops ?? Infinity));
  for (const node of byHops) {
    const info = orbits.byId.get(node.id);
    if (!info || info.hops === null || info.kind === "sun") {
      family.set(node.id, null);
      continue;
    }
    if (info.kind === "planet") {
      family.set(node.id, node.id);
      continue;
    }
    const hops = info.hops;
    const parents = [...(orbits.neighbours.get(node.id) ?? [])].filter((id) => orbits.byId.get(id)?.hops === hops - 1);
    const score = (id: string) => {
      const pair = pairOf(id, node.id);
      return [pair.holds ? 0 : 1, status.get(id) === "active" ? 0 : 1, -pair.weight, place.get(family.get(id) ?? "") ?? Infinity];
    };
    const parent = parents.sort((a, b) => {
      const [x, y] = [score(a), score(b)];
      return x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || x[3] - y[3] || a.localeCompare(b);
    })[0];
    family.set(node.id, parent ? (family.get(parent) ?? null) : null);
  }

  return { family, planetColor, status };
}

/** An entry's colour: the Sun's amber, its planet's colour (a moon's own is lightened from it), or slate. */
export function colorOf(id: string, orbits: Orbits, looks: Looks): string {
  const info = orbits.byId.get(id);
  if (info?.kind === "sun") return SUN_COLORS.body;
  const family = looks.family.get(id);
  const color = family ? looks.planetColor.get(family) : undefined;
  if (!color) return STRAY_COLOR;
  return info?.kind === "moon" ? lighten(color, MOON_LIGHTEN) : color;
}

/** A hex colour moved `amount` (0-1) towards white, as hex. */
export function lighten(hex: string, amount: number): string {
  return `#${[1, 3, 5]
    .map((at) => parseInt(hex.slice(at, at + 2), 16))
    .map((value) => Math.round(value + (255 - value) * amount).toString(16).padStart(2, "0"))
    .join("")}`;
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
