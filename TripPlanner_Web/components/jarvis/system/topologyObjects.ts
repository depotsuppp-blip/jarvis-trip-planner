import {
  AdditiveBlending,
  BufferGeometry,
  Float32BufferAttribute,
  Group,
  IcosahedronGeometry,
  Line,
  LineBasicMaterial,
  LineDashedMaterial,
  LineLoop,
  LineSegments,
  Mesh,
  MeshBasicMaterial,
  NormalBlending,
  SphereGeometry,
  Sprite,
  SpriteMaterial,
  Vector3,
  WireframeGeometry,
  type Intersection,
  type Object3D,
  type PerspectiveCamera,
  type Raycaster,
  type ShaderMaterial,
} from "three";
import { labelTexture, resources, rimMaterial } from "../memory/holoObjects";
import { CORE_COLORS, mixHex, OBSIDIAN, SLATE, WARNING, type ProviderStatus, type TopoKind, type TopoLinkKind } from "@/lib/jarvis/system";

/**
 * The Three.js pieces of the System Topology scene (browser only: the scene that imports this is
 * loaded with ssr: false). Built on the Memory page's holographic parts (holoObjects.ts: the
 * rim-lit core material, the lat/long wireframe globe, glow and ring textures, text labels), so
 * the two pages are one family. Each node is one group, built once and restyled in place.
 *
 *   JARVIS        a cyber-blue rim-lit core (#2E7BFF) inside a cyan icosahedron wireframe
 *                 (#00D2FF) that turns on two axes, a faint outer globe, a glow and two halo rings
 *   providers     a holographic wireframe globe in its family's colour (agents blue, MCP servers
 *                 violet, data & memory emerald, Jarvis's own tools cyan)
 *     auth needed an amber warning halo (#F59E0B) round the globe, breathing slowly
 *     offline     slate, at 30%
 *   capabilities  a small luminous orb in its provider's colour
 *   focus         a white ring round the open (or pointed-at) node
 *   labels        canvas-text sprites, the same size on screen at any distance
 *
 * Only an invisible hit sphere answers the pointer, and not while its node is filtered out.
 */

const LABEL_FADE = 0.35;

export interface TopoVisual {
  id: string;
  kind: TopoKind;
  radius: number;
  group: Group;
  core: Mesh<SphereGeometry, ShaderMaterial>;
  /** The spinning wireframe: the core's icosahedron, a provider's globe; none on a capability. */
  wire: LineSegments<BufferGeometry, LineBasicMaterial> | null;
  /** The core's faint outer globe. */
  shell: LineSegments<BufferGeometry, LineBasicMaterial> | null;
  glow: Sprite;
  halos: Sprite[];
  warn: Sprite;
  focus: Sprite;
  label: Sprite;
  labelKey: string;
  labelSize: [number, number];
  labelWanted: boolean;
  /** 0 the core, 1 an open, pointed-at or lit node, 2 a provider, 3 a capability. */
  labelPriority: number;
  /** auth_required: the warning halo breathes (see breathe). */
  warning: boolean;
  /** The strength the warning halo breathes round. */
  warnLevel: number;
}

export interface TopoStyle {
  visible: boolean;
  color: string;
  status: ProviderStatus;
  /** 1, or less while another node is lit up, a search leaves it out, or the filter puts it behind. */
  strength: number;
  selected: boolean;
  hovered: boolean;
  label: { text: string; color: string; weight: number; px: number; priority: number } | null;
}

let icosa: BufferGeometry | null = null;
function icosahedron(): BufferGeometry {
  icosa ??= new WireframeGeometry(new IcosahedronGeometry(1, 1));
  return icosa;
}

function sprite(map: SpriteMaterial["map"], additive: boolean): Sprite {
  return new Sprite(new SpriteMaterial({ map, transparent: true, depthWrite: false, blending: additive ? AdditiveBlending : NormalBlending }));
}

const noRaycast = (object: Object3D) => {
  object.raycast = () => {};
};

export function createTopoVisual(id: string, kind: TopoKind, radius: number): TopoVisual {
  const { sphere, hit, globe, glow, ring, dotted } = resources();
  const isCore = kind === "core";
  const group = new Group();
  group.userData = { nodeId: id, kind };

  const core = new Mesh(sphere, rimMaterial(!isCore));
  core.scale.setScalar(isCore ? radius * 0.62 : kind === "provider" ? radius * 0.9 : radius);

  let wire: TopoVisual["wire"] = null;
  let shell: TopoVisual["shell"] = null;
  if (isCore) {
    wire = new LineSegments(icosahedron(), new LineBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending }));
    wire.scale.setScalar(radius);
    shell = new LineSegments(globe, new LineBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending }));
    shell.scale.setScalar(radius * 1.45);
  } else if (kind === "provider") {
    wire = new LineSegments(globe, new LineBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending }));
    wire.scale.setScalar(radius);
  }

  const halo = sprite(glow, true);
  halo.scale.setScalar(radius * (isCore ? 6.5 : kind === "provider" ? 5 : 5.5));

  const halos = isCore
    ? [sprite(dotted, true), sprite(ring, true)].map((ringSprite, index) => {
        ringSprite.scale.setScalar(radius * (index === 0 ? 2.5 : 3.4));
        return ringSprite;
      })
    : [];

  const warn = sprite(ring, true);
  warn.scale.setScalar(radius * 2.7);

  const focus = sprite(ring, false);
  focus.scale.setScalar(radius * (isCore ? 3 : kind === "provider" ? 2.3 : 3.2));

  const label = new Sprite(new SpriteMaterial({ transparent: true, depthWrite: false, depthTest: false, sizeAttenuation: false }));
  label.renderOrder = 10;
  label.visible = false;

  const target = new Mesh(hit, new MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false }));
  target.scale.setScalar(Math.max(radius * 1.5, kind === "capability" ? 8 : 10));
  // Filtered out: not pointable either (the raycaster does not skip invisible groups).
  target.raycast = (raycaster: Raycaster, intersects: Intersection[]) => {
    if (group.visible) Mesh.prototype.raycast.call(target, raycaster, intersects);
  };

  const roles: [Object3D, string][] = [[halo, "glow"], [warn, "warn"], [core, "core"], [focus, "focus"], [label, "label"], [target, "hit"]];
  halos.forEach((ringSprite) => roles.push([ringSprite, "halo"]));
  if (wire) roles.push([wire, "wire"]);
  if (shell) roles.push([shell, "shell"]);
  for (const [object, role] of roles) object.userData.role = role;
  const parts = [halo, ...halos, warn, core, ...(wire ? [wire] : []), ...(shell ? [shell] : []), focus, label];
  for (const object of parts) noRaycast(object);
  group.add(...parts, target);

  return {
    id, kind, radius, group, core, wire, shell, glow: halo, halos, warn, focus, label,
    labelKey: "", labelSize: [0, 0], labelWanted: false, labelPriority: 3, warning: false, warnLevel: 0,
  };
}

function setOpacity(object: Sprite | LineSegments<BufferGeometry, LineBasicMaterial>, opacity: number) {
  object.visible = opacity > 0.004;
  object.material.opacity = opacity;
}

/** Restyles one node in place; `pixel` turns CSS pixels into label sprite units. */
export function styleTopoVisual(visual: TopoVisual, style: TopoStyle, pixel: number): void {
  const { group, core, wire, shell, glow, halos, warn, focus } = visual;
  group.visible = style.visible;
  if (!style.visible) return;
  const offline = style.status === "offline";
  const auth = style.status === "auth_required";
  // Offline recedes to 30% and turns slate.
  const s = style.strength * (offline ? 0.3 : 1);
  const color = offline ? mixHex(style.color, SLATE, 0.75) : style.color;
  const uniforms = core.material.uniforms;

  if (visual.kind === "core") {
    uniforms.uCore.value.set(mixHex(CORE_COLORS.body, OBSIDIAN, 0.35));
    uniforms.uRim.value.set(offline ? SLATE : CORE_COLORS.wire);
    uniforms.uPower.value = 1.5;
    uniforms.uOpacity.value = s;
    if (wire) {
      wire.material.color.set(offline ? SLATE : CORE_COLORS.wire);
      setOpacity(wire, 0.85 * s);
    }
    if (shell) {
      shell.material.color.set(offline ? SLATE : CORE_COLORS.body);
      setOpacity(shell, 0.22 * s);
    }
    glow.material.color.set(offline ? SLATE : CORE_COLORS.body);
    setOpacity(glow, 0.8 * s);
    halos.forEach((ringSprite, index) => {
      ringSprite.material.color.set(index === 0 ? CORE_COLORS.wire : CORE_COLORS.body);
      setOpacity(ringSprite, (index === 0 ? 0.5 : 0.28) * s);
    });
  } else {
    const provider = visual.kind === "provider";
    uniforms.uCore.value.set(mixHex(color, OBSIDIAN, provider ? 0.6 : 0.35));
    uniforms.uRim.value.set(color);
    uniforms.uPower.value = provider ? 2 : 1.1;
    uniforms.uOpacity.value = (provider ? 0.95 : 0.9) * s;
    if (wire) {
      wire.material.color.set(color);
      setOpacity(wire, 0.75 * s);
    }
    glow.material.color.set(auth ? WARNING : color);
    setOpacity(glow, offline ? 0 : (provider ? 0.5 : 0.42) * s);
  }

  visual.warning = auth;
  visual.warnLevel = auth ? 0.9 * style.strength : 0;
  warn.material.color.set(WARNING);
  setOpacity(warn, visual.warnLevel);

  focus.material.color.set("#ffffff");
  setOpacity(focus, style.selected ? 0.95 : style.hovered ? 0.5 : 0);

  const label = style.label;
  visual.labelWanted = label !== null;
  if (!label) {
    visual.label.visible = false;
    return;
  }
  visual.labelPriority = label.priority;
  const key = `${label.text}|${label.color}|${label.weight}|${label.px}`;
  if (key !== visual.labelKey) {
    visual.label.material.map?.dispose();
    const drawn = labelTexture(label.text, label.color, label.weight, label.px);
    visual.label.material.map = drawn.map;
    visual.label.material.needsUpdate = true;
    visual.labelKey = key;
    visual.labelSize = [drawn.width, drawn.height];
  }
  visual.label.scale.set(visual.labelSize[0] * pixel, visual.labelSize[1] * pixel, 1);
  visual.label.material.opacity = Math.max(style.strength, LABEL_FADE);
}

/** One frame of motion: the core's wireframe turns on two axes, globes on one, warning halos breathe. */
export function animate(visual: TopoVisual, step: number, time: number): void {
  if (visual.wire) {
    visual.wire.rotation.y += step;
    if (visual.kind === "core") visual.wire.rotation.x += step * 0.35;
  }
  if (visual.shell) visual.shell.rotation.y -= step * 0.4;
  visual.halos.forEach((ringSprite, index) => {
    ringSprite.material.rotation += (index === 0 ? 0.5 : -0.35) * step;
  });
  if (visual.warning) setOpacity(visual.warn, visual.warnLevel * (0.6 + 0.4 * Math.sin(time / 600)));
}

const at = new Vector3();

/**
 * Places every wanted label for this frame: under its orb, else over it, never on top of a label
 * already placed - higher priority first. Labels of priority `forced` or better always show (in
 * their first spot if none is free): the core's, the lit nodes' and - on a wide screen - the
 * providers'; the rest wait for room.
 */
export function placeTopoLabels(visuals: Iterable<TopoVisual>, camera: PerspectiveCamera, width: number, height: number, forced = 2): void {
  const t = Math.tan((camera.fov * Math.PI) / 360);
  const items: { visual: TopoVisual; x: number; y: number; r: number }[] = [];
  for (const visual of visuals) {
    if (!visual.labelWanted || !visual.group.visible) {
      visual.label.visible = false;
      continue;
    }
    at.copy(visual.group.position);
    const distance = camera.position.distanceTo(at);
    at.project(camera);
    if (at.z > 1 || at.z < -1) {
      visual.label.visible = false;
      continue;
    }
    const r = ((visual.radius * (visual.kind === "core" ? 1.5 : 1.1)) / (distance * t)) * (height / 2);
    items.push({ visual, x: ((at.x + 1) / 2) * width, y: ((1 - at.y) / 2) * height, r });
  }
  items.sort((a, b) => a.visual.labelPriority - b.visual.labelPriority);
  const boxes: [number, number, number, number][] = [];
  for (const { visual, x, y, r } of items) {
    const [w, h] = visual.labelSize;
    const spots = [1, -1].map((side) => {
      const oy = side * (r + 4 + h / 2);
      return { oy, box: [x, y + oy, w / 2 + 2, h / 2] as [number, number, number, number] };
    });
    const free = (box: [number, number, number, number]) =>
      !boxes.some(([bx, by, bw, bh]) => Math.abs(bx - box[0]) < bw + box[2] && Math.abs(by - box[1]) < bh + box[3]);
    let spot = spots.find((candidate) => free(candidate.box));
    if (!spot && visual.labelPriority <= forced) spot = spots[0];
    if (!spot) {
      visual.label.visible = false;
      continue;
    }
    boxes.push(spot.box);
    visual.label.center.set(0.5, 0.5 + spot.oy / h);
    visual.label.visible = true;
  }
}

// ---- links ------------------------------------------------------------------------------------------

export interface LinkVisual {
  line: Line<BufferGeometry, LineBasicMaterial | LineDashedMaterial>;
  dashed: boolean;
}

/** One link's line: solid while data can flow, dashed and muted when an end cannot work. */
export function createLinkVisual(active: boolean): LinkVisual {
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute([0, 0, 0, 0, 0, 0], 3));
  const material = active
    ? new LineBasicMaterial({ transparent: true, depthWrite: false })
    : new LineDashedMaterial({ transparent: true, depthWrite: false, dashSize: 4, gapSize: 5 });
  const line = new Line(geometry, material);
  line.userData.role = active ? "link" : "link-muted";
  noRaycast(line);
  return { line, dashed: !active };
}

/** Puts a link's line between its two ends (the nodes are pinned, so this runs once per layout). */
export function placeLink(visual: LinkVisual, start: { x: number; y: number; z: number }, end: { x: number; y: number; z: number }): void {
  const position = visual.line.geometry.getAttribute("position") as Float32BufferAttribute;
  position.setXYZ(0, start.x, start.y, start.z);
  position.setXYZ(1, end.x, end.y, end.z);
  position.needsUpdate = true;
  visual.line.geometry.computeBoundingSphere();
  if (visual.dashed) visual.line.computeLineDistances();
}

const LINK_ALPHA: Record<TopoLinkKind, number> = { provider: 0.42, capability: 0.26, uses: 0.2 };

export function styleLink(visual: LinkVisual, kind: TopoLinkKind, color: string, strength: number): void {
  const material = visual.line.material;
  material.color.set(visual.dashed ? SLATE : color);
  material.opacity = (visual.dashed ? 0.32 : LINK_ALPHA[kind]) * strength;
  visual.line.visible = material.opacity > 0.004;
}

// The library does not add a custom particle object itself: it takes its geometry and material and
// builds a Mesh from them for every particle (a Sprite's material on a Mesh breaks the render). So a
// particle is a small sphere in an additive, unlit material - bright where it overlaps its line,
// which reads as a glow - and each size and colour is made once: the library disposes a particle's
// geometry or material only when a link is given a different one.
const particleGeometries = new Map<number, SphereGeometry>();
const particleMaterials = new Map<string, MeshBasicMaterial>();

/** A glowing particle for the data flowing along a link. */
export function createParticle(color: string, radius: number): Mesh<SphereGeometry, MeshBasicMaterial> {
  let geometry = particleGeometries.get(radius);
  if (!geometry) {
    geometry = new SphereGeometry(radius, 10, 8);
    particleGeometries.set(radius, geometry);
  }
  let material = particleMaterials.get(color);
  if (!material) {
    material = new MeshBasicMaterial({ color, transparent: true, opacity: 0.95, blending: AdditiveBlending, depthWrite: false });
    particleMaterials.set(color, material);
  }
  return new Mesh(geometry, material);
}

// ---- the orbit rings -----------------------------------------------------------------------------------

/** A faint ring on the orbital plane for the providers' orbit, a fainter dashed one for each tools row. */
export function createTopoRings(radii: number[]): Group {
  const rings = new Group();
  rings.userData.role = "orbits";
  radii.forEach((radius, index) => {
    const points: number[] = [];
    for (let s = 0; s < 180; s++) {
      const a = (s / 180) * 2 * Math.PI;
      points.push(Math.cos(a) * radius, 0, Math.sin(a) * radius);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(points, 3));
    const material =
      index === 0
        ? new LineBasicMaterial({ color: 0x5aa8ff, transparent: true, opacity: 0.2, depthWrite: false })
        : new LineDashedMaterial({ color: 0x8b9bd9, transparent: true, opacity: 0.1, dashSize: 4, gapSize: 10, depthWrite: false });
    const loop = new LineLoop(geometry, material);
    if (index > 0) loop.computeLineDistances();
    noRaycast(loop);
    rings.add(loop);
  });
  return rings;
}

export function disposeRings(rings: Group): void {
  rings.traverse((object) => {
    const line = object as LineLoop;
    line.geometry?.dispose();
    (line.material as { dispose?: () => void } | undefined)?.dispose?.();
  });
}
