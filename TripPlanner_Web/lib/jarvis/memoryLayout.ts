/**
 * Where every entry of the Memory page's 3D solar system sits - worked out once, then pinned.
 * Pure (d3-force, no DOM), run when the graph loads and again after something is deleted, never
 * while it is on screen: the scene only draws what this decides, so nothing drifts.
 *
 *   1. On the flat orbital plane (x, z): every entry seeded on its ring (seedOrbits), then
 *      settled by d3-force - forceRadial holds each ring (hops from the centre), a collision
 *      force and a spread along each ring space them, weak links pull each moon towards the
 *      planet it hangs off, and the centre is pinned at the origin.
 *   2. Lifted off the plane (y) by a small, stable amount per entry - planets a little, moons
 *      more, the outer orbit below - so the system has depth when the camera turns round it.
 *   3. Pinned: fx, fy, fz = x, y, z.
 */

import { forceCollide, forceLink, forceManyBody, forceRadial, forceSimulation, type Simulation } from "d3-force";
import { isMe } from "./memory";
import {
  computeOrbits,
  endId,
  seedOrbits,
  type GraphLink,
  type GraphNode,
  type Orbits,
  type PlaneNode,
  type SolarGraph,
} from "./memoryOrbits";

export const SUN_RADIUS = 40;
const CENTRE_RADIUS = 28;
// The least room between two moons on one ring (graph units of arc).
const MOON_GAP = 64;
const TICKS = 300;
// How far entries rise off the orbital plane (graph units).
const PLANET_LIFT = 28;
const MOON_LIFT = [26, 70] as const;
const DRIFT_DROP = 50;

/** An entry's radius in graph units: the Sun, then planets and moons by how many relations they have. */
export function nodeRadius(node: Pick<GraphNode, "id" | "name">, orbits: Orbits): number {
  const kind = orbits.byId.get(node.id)?.kind ?? "moon";
  const links = Math.sqrt(orbits.degree.get(node.id) ?? 0);
  if (kind === "sun") return isMe(node) ? SUN_RADIUS : CENTRE_RADIUS;
  if (kind === "planet") return 20 + Math.min(10, links * 3);
  return 9 + Math.min(5, links * 2);
}

/** A stable number in [0, 1) from an id (FNV-1a), so an entry's height never changes between loads. */
function unit(id: string, salt = 0): number {
  let hash = 2166136261 ^ salt;
  for (let index = 0; index < id.length; index++) hash = Math.imul(hash ^ id.charCodeAt(index), 16777619);
  return (hash >>> 0) / 4294967296;
}

/** How far an entry sits above (+) or below (-) the orbital plane. */
function lift(id: string, orbits: Orbits): number {
  const info = orbits.byId.get(id);
  if (!info || info.kind === "sun") return 0;
  if (info.hops === null) return -DRIFT_DROP - unit(id, 1) * 16;
  if (info.kind === "planet") return (unit(id, 2) - 0.5) * 2 * PLANET_LIFT;
  const side = unit(id, 3) < 0.5 ? -1 : 1;
  return side * (MOON_LIFT[0] + unit(id, 4) * (MOON_LIFT[1] - MOON_LIFT[0]));
}

/**
 * Keeps the entries on one ring apart along it - a collision in one dimension, round each
 * circle, that keeps the order they were seeded in: planets at least 88% of an even share of the
 * inner orbit (room for every name), moons at least a moon's width (so they still gather by the
 * planet they hang off).
 */
function forceSpread(orbits: Orbits, strength = 0.9) {
  let nodes: PlaneNode[] = [];
  const force = (alpha: number) => {
    const rings = new Map<number, { node: PlaneNode; angle: number }[]>();
    for (const node of nodes) {
      const ring = orbits.byId.get(node.id)?.ring ?? 0;
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
  force.initialize = (all: PlaneNode[]) => {
    nodes = all;
  };
  return force;
}

/**
 * Places every entry (see the module comment). An entry that already has a place starts from it,
 * so after a delete the rest only close the gap. Mutates the nodes: call it outside rendering
 * (the load, a state updater).
 */
export function layoutSystem(nodes: GraphNode[], links: readonly GraphLink[], orbits: Orbits): void {
  // The plane: x stays x, the scene's z is the plane's y.
  const plane: PlaneNode[] = nodes.map((node) => ({
    id: node.id,
    name: node.name,
    type: node.type,
    description: node.description,
    x: node.x,
    y: node.z,
  }));
  seedOrbits(plane, orbits);
  const byId = new Map(plane.map((node) => [node.id, node]));
  const ids = new Set(byId.keys());
  const ringOf = (node: PlaneNode) => orbits.byId.get(node.id)?.ring ?? 0;
  const radiusAt = (ring: number) => orbits.radii[ring] ?? 0;
  const planeLinks = links
    .map((link) => ({ source: endId(link.source), target: endId(link.target) }))
    .filter((link) => ids.has(link.source) && ids.has(link.target) && link.source !== link.target);

  const simulation: Simulation<PlaneNode, undefined> = forceSimulation<PlaneNode>(plane)
    .force(
      "link",
      forceLink<PlaneNode, { source: string | PlaneNode; target: string | PlaneNode }>(planeLinks)
        .id((node) => node.id)
        .distance((link) => {
          const a = ringOf(link.source as PlaneNode);
          const b = ringOf(link.target as PlaneNode);
          return a === b ? 70 : Math.max(60, Math.abs(radiusAt(a) - radiusAt(b)));
        })
        .strength(0.1),
    )
    .force("charge", forceManyBody<PlaneNode>().strength(-110).distanceMax(280))
    .force("orbit", forceRadial<PlaneNode>((node) => radiusAt(ringOf(node)), 0, 0).strength((node) => (ringOf(node) === 0 ? 0 : 0.85)))
    .force("collide", forceCollide<PlaneNode>((node) => nodeRadius(node, orbits) + 6).strength(0.8))
    .force("spread", forceSpread(orbits))
    .stop();
  for (let tick = 0; tick < TICKS; tick++) simulation.tick();

  for (const node of nodes) {
    const flat = byId.get(node.id);
    const x = flat?.x ?? 0;
    const z = flat?.y ?? 0;
    const y = lift(node.id, orbits);
    node.x = node.fx = x;
    node.y = node.fy = y;
    node.z = node.fz = z;
    node.vx = node.vy = node.vz = 0;
  }
}

/**
 * After entries or relations go, the graph that is left, laid out again (the centre can change
 * when there is no "me"; an entry can change orbit). Call it in a state updater.
 */
export function settle(nodes: GraphNode[], links: GraphLink[]): SolarGraph {
  layoutSystem(nodes, links, computeOrbits(nodes, links));
  return { nodes, links };
}
