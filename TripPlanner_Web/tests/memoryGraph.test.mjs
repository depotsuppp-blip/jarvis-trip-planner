// Regression tests for the Memory page's graph (/jarvis/memory): what the server sends is made
// safe (lib/jarvis/memory.ts readMemoryGraph) and every entry gets a place the 3D scene can draw
// (lib/jarvis/memoryOrbits.ts, lib/jarvis/memoryLayout.ts) - unlinked entries and numeric names
// such as "0.0812" included. Pure: no browser, no server.
//
//     npm test          (node --test tests/)
//
// Node runs the TypeScript sources itself (type stripping); the hook below only adds the ".ts"
// that the app's extensionless imports leave out.

import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier)) {
      try {
        return next(`${specifier}.ts`, context);
      } catch {
        // not a .ts module: resolved as written below
      }
    }
    return next(specifier, context);
  },
});

const { readMemoryGraph, readMemoryNode, displayName, isMe, UNNAMED } = await import("../lib/jarvis/memory.ts");
const { computeOrbits, computeLooks, colorOf, STRAY_COLOR } = await import("../lib/jarvis/memoryOrbits.ts");
const { layoutSystem, isPlaced } = await import("../lib/jarvis/memoryLayout.ts");

const link = (id, source, target, relation_type = "related_to", status = "active") => ({
  id, source, target, relation_type, weight: 1, status, updated_at: "2026-11-20T06:00:00Z",
});

/** The owner's memory after the "tgo CFO.pdf" upload (2026-11-20): 10 entries, 9 relations, 6 not linked to "me". */
function liveGraph() {
  return {
    nodes: [
      { id: "n1", name: "0.0812", type: "other", description: "Electricity, grid mix (ไฟฟ้า) emission factor 0.0812" },
      { id: "n2", name: "0.0987", type: "other", description: "emission factor" },
      { id: "n3", name: "0.4750", type: "other", description: "emission factor" },
      { id: "n4", name: "0.4999", type: "other", description: "emission factor" },
      { id: "n5", name: "0.5986", type: "other", description: "emission factor" },
      { id: "n6", name: "Electricity, grid mix (ไฟฟ้า)", type: "other", description: "grid electricity" },
      { id: "n7", name: "FastAPI", type: "thing", description: "the backend framework" },
      { id: "n8", name: "me", type: "person", description: "The person Jarvis is talking to (the user)." },
      { id: "n9", name: "Subtrack", type: "project", description: "a project the user is building" },
      { id: "n10", name: "งาน A", type: "project", description: "the new project the user is starting" },
    ],
    links: [
      link("l1", "n9", "n7", "backend_written_in"),
      link("l2", "n10", "n8", "building"),
      ...["n1", "n5", "n3", "n4", "n2"].map((id, index) => link(`l${3 + index}`, "n6", id, "emission_factor")),
      link("l8", "n8", "n10", "building"),
      link("l9", "n8", "n9", "building", "completed"),
    ],
  };
}

function laidOut(raw) {
  const graph = readMemoryGraph(raw);
  const orbits = computeOrbits(graph.nodes, graph.links);
  layoutSystem(graph.nodes, graph.links, orbits);
  return { graph, orbits };
}

function assertAllPlaced(nodes) {
  const unplaced = nodes.filter((node) => !isPlaced(node));
  assert.deepEqual(unplaced.map((node) => node.name), [], "every entry has three finite coordinates");
  for (const node of nodes) assert.deepEqual([node.fx, node.fy, node.fz], [node.x, node.y, node.z], `${node.name} is pinned where it is`);
}

test("the live graph: unlinked numeric entries are placed on the outer orbit, below the plane", () => {
  const { graph, orbits } = laidOut(liveGraph());
  assert.equal(graph.nodes.length, 10);
  assert.equal(graph.links.length, 9);
  assert.deepEqual(orbits.counts, { planets: 2, moons: 1, drifting: 6 });
  assert.equal(orbits.driftRing, 3);
  assertAllPlaced(graph.nodes);
  for (const id of ["n1", "n2", "n3", "n4", "n5", "n6"]) {
    const node = graph.nodes.find((item) => item.id === id);
    const distance = Math.hypot(node.x, node.z);
    assert.ok(Math.abs(distance - orbits.radii[3]) < orbits.radii[3] * 0.35, `${node.name} near the outer orbit (${distance.toFixed(0)} vs ${orbits.radii[3]})`);
    assert.ok(node.y < 0, `${node.name} below the plane`);
  }
  // No two entries on top of each other.
  for (const a of graph.nodes) {
    for (const b of graph.nodes) {
      if (a.id < b.id) assert.ok(Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z) > 4, `${a.name} and ${b.name} apart`);
    }
  }
  // Unlinked entries are slate; their status is their own fact's.
  const looks = computeLooks(graph.nodes, graph.links, orbits);
  assert.equal(colorOf("n1", orbits, looks), STRAY_COLOR);
  assert.equal(looks.status.get("n1"), "active");
  assert.equal(looks.status.get("n9"), "completed");
});

test("a graph from the server is made safe: strings everywhere, no broken relations", () => {
  const graph = readMemoryGraph({
    nodes: [
      { id: "a", name: 0.0812, type: null, description: undefined },
      { id: 7, name: "seven", type: "other", description: 12 },
      { id: "c", name: null, type: "project", description: "x" },
      { id: "a", name: "duplicate id", type: "other", description: "" },
      { name: "no id" },
      null,
      "junk",
      { id: "me", name: "Me", type: "person", description: "" },
    ],
    links: [
      link("l1", "me", "a"),
      link("l1", "me", "7"), // the same id twice: both kept, under two ids
      link("l3", "me", "missing"), // an end that is not there
      link("l4", "c", "c"), // a relation to itself
      { id: "l5", source: "a", target: "c", relation_type: 5, weight: Number.NaN, status: "maybe", updated_at: 3 },
      null,
    ],
  });
  assert.deepEqual(graph.nodes, [
    { id: "a", name: "0.0812", type: "other", description: "" },
    { id: "7", name: "seven", type: "other", description: "12" },
    { id: "c", name: UNNAMED, type: "project", description: "x" },
    { id: "me", name: "Me", type: "person", description: "" },
  ]);
  assert.deepEqual(graph.links.map((item) => [item.id, item.source, item.target]), [
    ["l1", "me", "a"],
    ["l1'", "me", "7"],
    ["l5", "a", "c"],
  ]);
  const odd = graph.links[2];
  assert.deepEqual([odd.relation_type, odd.weight, odd.status, odd.updated_at], ["5", 1, "active", null]);
  assert.deepEqual(readMemoryGraph(null), { nodes: [], links: [] });
  assert.deepEqual(readMemoryGraph({ nodes: "x", links: {} }), { nodes: [], links: [] });
  assert.equal(readMemoryNode({ id: "  " }), null);
  assert.equal(isMe({ name: 42 }), false);
  assert.equal(displayName({ name: "" }), UNNAMED);
  assert.equal(displayName({ name: "me" }), "me (you)");
});

test("malformed shapes still lay out: numeric names, no 'me', a long chain, a NaN start", () => {
  const nodes = [
    { id: "p", name: 2027, type: "project", description: null },
    { id: "q", name: "Q", type: "", description: "" },
  ];
  const links = [link("pq", "p", "q")];
  let previous = "q";
  for (let index = 1; index <= 7; index++) {
    nodes.push({ id: `c${index}`, name: (index / 10).toFixed(4), type: "other", description: "" });
    links.push(link(`k${index}`, previous, `c${index}`));
    previous = `c${index}`;
  }
  nodes.push({ id: "alone", name: "0.0001", type: "other", description: "" });
  const { graph, orbits } = laidOut({ nodes, links });
  assert.equal(orbits.centerIsMe, false, "with no 'me' the most connected entry is the centre");
  assertAllPlaced(graph.nodes);

  // A re-layout (after a delete) starts from the places the entries had: a NaN one must not spread.
  graph.nodes[3].x = Number.NaN;
  graph.nodes[3].z = Number.NaN;
  layoutSystem(graph.nodes, graph.links, orbits);
  assertAllPlaced(graph.nodes);
});

test("an empty graph and a single entry", () => {
  const empty = laidOut({ nodes: [], links: [] });
  assert.equal(empty.orbits.centerId, null);
  const single = laidOut({ nodes: [{ id: "x", name: "0.5", type: "other", description: "" }], links: [] });
  assertAllPlaced(single.graph.nodes);
  assert.deepEqual([single.graph.nodes[0].x, single.graph.nodes[0].z], [0, 0], "a lone entry is the centre");
});
