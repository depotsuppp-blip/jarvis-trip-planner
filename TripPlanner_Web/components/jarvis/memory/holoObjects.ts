import {
  AdditiveBlending,
  BufferGeometry,
  CanvasTexture,
  Color,
  Float32BufferAttribute,
  Group,
  LineBasicMaterial,
  LineDashedMaterial,
  LineLoop,
  LineSegments,
  Mesh,
  MeshBasicMaterial,
  NormalBlending,
  ShaderMaterial,
  SphereGeometry,
  Sprite,
  SpriteMaterial,
  SRGBColorSpace,
  UniformsLib,
  UniformsUtils,
  Vector3,
  type Object3D,
  type PerspectiveCamera,
  type Raycaster,
  type Intersection,
} from "three";
import type { RelationStatus } from "@/lib/jarvis/memory";
import { mixHex, OBSIDIAN, SUN_COLORS, type OrbitKind, type Orbits } from "@/lib/jarvis/memoryOrbits";

/**
 * The Three.js pieces of the Memory page's holographic solar system (browser only: the scene
 * module that imports this is loaded with ssr: false). Each entry is one group, built once and
 * then restyled in place - colour, status, focus and labels change materials, never the objects,
 * so the graph library never rebuilds the scene.
 *
 *   the Sun   a white-gold rim-lit core (#FFF7CC warming to #FFB703), a golden-orange wireframe
 *             globe round it (#FF8C24, 45%, additive), a soft glow sprite and two halo rings
 *   planets   a rim-lit core in the family's colour (dark at the centre, bright at the edge),
 *             a latitude/longitude wireframe globe in the same colour (additive) and a glow sprite
 *   moons     a smaller translucent sphere in a deeper tint of the family, a faint glow
 *   status    Active at full strength; Done calmer, inside a crisp emerald ring (#10B981) with a
 *             check badge on its top-right edge; Archived a muted slate wireframe at 25%
 *   focus     a white ring round the open (or pointed-at) entry
 *   labels    a canvas-text sprite under the orb, the same size on screen at any distance
 *
 * Only an invisible hit sphere answers the pointer (glows and labels never steal a click), and it
 * stops answering while its entry is hidden by the type filter.
 */

export const DONE = "#10B981";
const DONE_INK = "#022C22";
const SLATE = "#94A3B8";
const SLATE_DEEP = "#64748B";
const LABEL_HALO = "rgba(6, 7, 11, 0.86)";

// ---- shared geometry and textures (made once, in the browser) ---------------------------------

export interface Shared {
  sphere: SphereGeometry;
  hit: SphereGeometry;
  globe: BufferGeometry;
  glow: CanvasTexture;
  ring: CanvasTexture;
  dotted: CanvasTexture;
  check: CanvasTexture;
}

let shared: Shared | null = null;

function canvas(width: number, height: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const element = document.createElement("canvas");
  element.width = width;
  element.height = height;
  const ctx = element.getContext("2d");
  if (!ctx) throw new Error("2D canvas unavailable");
  return [element, ctx];
}

function texture(element: HTMLCanvasElement): CanvasTexture {
  const map = new CanvasTexture(element);
  map.colorSpace = SRGBColorSpace;
  return map;
}

/** A unit globe drawn as latitude circles and meridians only (no triangle diagonals). */
function latLongGeometry(latitudes: number, meridians: number, segments: number): BufferGeometry {
  const points: number[] = [];
  for (let i = 1; i < latitudes; i++) {
    const phi = (i / latitudes) * Math.PI;
    const y = Math.cos(phi);
    const r = Math.sin(phi);
    for (let s = 0; s < segments; s++) {
      const a = (s / segments) * 2 * Math.PI;
      const b = ((s + 1) / segments) * 2 * Math.PI;
      points.push(r * Math.cos(a), y, r * Math.sin(a), r * Math.cos(b), y, r * Math.sin(b));
    }
  }
  const half = segments / 2;
  for (let j = 0; j < meridians; j++) {
    const theta = (j / meridians) * 2 * Math.PI;
    for (let s = 0; s < half; s++) {
      const p = (s / half) * Math.PI;
      const q = ((s + 1) / half) * Math.PI;
      points.push(
        Math.sin(p) * Math.cos(theta), Math.cos(p), Math.sin(p) * Math.sin(theta),
        Math.sin(q) * Math.cos(theta), Math.cos(q), Math.sin(q) * Math.sin(theta),
      );
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute(points, 3));
  return geometry;
}

function glowTexture(): CanvasTexture {
  const [element, ctx] = canvas(128, 128);
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, "rgba(255, 255, 255, 1)");
  gradient.addColorStop(0.18, "rgba(255, 255, 255, 0.62)");
  gradient.addColorStop(0.42, "rgba(255, 255, 255, 0.2)");
  gradient.addColorStop(1, "rgba(255, 255, 255, 0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  return texture(element);
}

function ringTexture(dotted: boolean): CanvasTexture {
  const [element, ctx] = canvas(256, 256);
  ctx.strokeStyle = "#ffffff";
  ctx.fillStyle = "#ffffff";
  if (dotted) {
    for (let i = 0; i < 72; i++) {
      const a = (i / 72) * 2 * Math.PI;
      ctx.beginPath();
      ctx.arc(128 + Math.cos(a) * 112, 128 + Math.sin(a) * 112, i % 6 === 0 ? 3.2 : 1.8, 0, 2 * Math.PI);
      ctx.fill();
    }
  } else {
    ctx.shadowColor = "rgba(255, 255, 255, 0.8)";
    ctx.shadowBlur = 6;
    ctx.lineWidth = 5;
    ctx.beginPath();
    ctx.arc(128, 128, 112, 0, 2 * Math.PI);
    ctx.stroke();
  }
  return texture(element);
}

function checkTexture(): CanvasTexture {
  const [element, ctx] = canvas(128, 128);
  ctx.fillStyle = OBSIDIAN;
  ctx.beginPath();
  ctx.arc(64, 64, 62, 0, 2 * Math.PI);
  ctx.fill();
  ctx.fillStyle = DONE;
  ctx.beginPath();
  ctx.arc(64, 64, 54, 0, 2 * Math.PI);
  ctx.fill();
  ctx.strokeStyle = DONE_INK;
  ctx.lineWidth = 14;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(38, 66);
  ctx.lineTo(56, 84);
  ctx.lineTo(91, 46);
  ctx.stroke();
  return texture(element);
}

/** The shared geometry and textures (made once, in the browser); the System Topology scene draws with them too. */
export function resources(): Shared {
  shared ??= {
    sphere: new SphereGeometry(1, 40, 28),
    hit: new SphereGeometry(1, 10, 8),
    globe: latLongGeometry(9, 14, 72),
    glow: glowTexture(),
    ring: ringTexture(false),
    dotted: ringTexture(true),
    check: checkTexture(),
  };
  return shared;
}

// ---- the rim-lit core ---------------------------------------------------------------------------

// Dark at the centre, bright at the edge (a fresnel rim): the holographic globe's body. With the
// scene's fog, so a far planet fades into the dark like the rest.
const RIM_VERTEX = /* glsl */ `
#include <fog_pars_vertex>
varying vec3 vNormal;
varying vec3 vView;
void main() {
  vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
  vNormal = normalize(normalMatrix * normal);
  vView = normalize(-mvPosition.xyz);
  gl_Position = projectionMatrix * mvPosition;
  #include <fog_vertex>
}
`;

const RIM_FRAGMENT = /* glsl */ `
uniform vec3 uCore;
uniform vec3 uRim;
uniform float uOpacity;
uniform float uPower;
varying vec3 vNormal;
varying vec3 vView;
#include <fog_pars_fragment>
void main() {
  float facing = max(dot(normalize(vNormal), normalize(vView)), 0.0);
  float rim = pow(1.0 - facing, uPower);
  gl_FragColor = vec4(mix(uCore, uRim, rim), uOpacity * mix(0.55, 1.0, rim));
  #include <colorspace_fragment>
  #include <fog_fragment>
}
`;

export function rimMaterial(additive: boolean): ShaderMaterial {
  return new ShaderMaterial({
    uniforms: UniformsUtils.merge([
      UniformsLib.fog,
      { uCore: { value: new Color() }, uRim: { value: new Color() }, uOpacity: { value: 1 }, uPower: { value: 2 } },
    ]),
    vertexShader: RIM_VERTEX,
    fragmentShader: RIM_FRAGMENT,
    transparent: true,
    depthWrite: !additive,
    blending: additive ? AdditiveBlending : NormalBlending,
    fog: true,
  });
}

function sprite(map: CanvasTexture, additive: boolean): Sprite {
  const result = new Sprite(
    new SpriteMaterial({ map, transparent: true, depthWrite: false, blending: additive ? AdditiveBlending : NormalBlending }),
  );
  return result;
}

// Glows, rings and labels never answer the pointer; only the hit sphere does.
const noRaycast = (object: Object3D) => {
  object.raycast = () => {};
};

// ---- one entry ------------------------------------------------------------------------------------

export interface NodeVisual {
  id: string;
  kind: OrbitKind;
  /** The Sun's look (the centre), whether or not it is "me". */
  sun: boolean;
  radius: number;
  group: Group;
  core: Mesh<SphereGeometry, ShaderMaterial>;
  /** The wireframe globe that spins (the Sun and planets). */
  wire: LineSegments<BufferGeometry, LineBasicMaterial> | null;
  glow: Sprite;
  /** The Sun's two halo rings (they turn slowly, opposite ways). */
  halos: Sprite[];
  done: Sprite;
  badge: Sprite;
  focus: Sprite;
  label: Sprite;
  labelKey: string;
  labelSize: [number, number];
  /** Asked for by the style; placeLabels decides where (and whether) it shows this frame. */
  labelWanted: boolean;
  /** 0 the Sun, 1 an open, pointed-at or lit entry, 2 a planet, 3 a moon: who keeps their spot. */
  labelPriority: number;
}

export interface VisualStyle {
  /** Shown (the type filter keeps it). */
  visible: boolean;
  /** Its colour: the family's, a moon's deeper tint, or slate. */
  color: string;
  state: RelationStatus;
  /** 1, or less while another entry is lit up or a search leaves it out. */
  strength: number;
  selected: boolean;
  hovered: boolean;
  label: { text: string; color: string; weight: number; px: number; priority: number } | null;
}

export function createVisual(id: string, kind: OrbitKind, radius: number): NodeVisual {
  const { sphere, hit, globe, glow, ring, dotted, check } = resources();
  const sun = kind === "sun";
  const group = new Group();
  group.userData = { nodeId: id, kind };

  const core = new Mesh(sphere, rimMaterial(!sun));
  core.scale.setScalar(sun ? radius : kind === "planet" ? radius * 0.92 : radius);
  core.userData.role = "core";

  let wire: NodeVisual["wire"] = null;
  if (kind !== "moon") {
    wire = new LineSegments(
      globe,
      new LineBasicMaterial({ transparent: true, depthWrite: false, blending: AdditiveBlending }),
    );
    wire.scale.setScalar(sun ? radius * 1.32 : radius);
    wire.userData.role = "wire";
  }

  const halo = sprite(glow, true);
  halo.scale.setScalar(radius * (sun ? 7.5 : kind === "planet" ? 5 : 4));
  halo.userData.role = "glow";

  const halos = sun
    ? [sprite(dotted, true), sprite(ring, true)].map((ringSprite, index) => {
        ringSprite.scale.setScalar(radius * (index === 0 ? 3.1 : 4.3));
        ringSprite.userData.role = "halo";
        return ringSprite;
      })
    : [];

  const done = sprite(ring, false);
  done.scale.setScalar(radius * 2.9);
  done.userData.role = "done";

  // The check badge sits on the orb's top-right edge on screen, whichever way the camera looks.
  const badge = sprite(check, false);
  const badgeSize = Math.max(radius * 0.75, 3);
  badge.scale.setScalar(badgeSize);
  badge.center.set(0.5 - (radius * 0.72) / badgeSize, 0.5 - (radius * 0.72) / badgeSize);
  badge.renderOrder = 5;
  badge.userData.role = "badge";

  const focus = sprite(ring, false);
  focus.scale.setScalar(radius * (sun ? 3.6 : 2.5));
  focus.userData.role = "focus";

  const label = new Sprite(new SpriteMaterial({ transparent: true, depthWrite: false, depthTest: false, sizeAttenuation: false }));
  label.renderOrder = 10;
  label.visible = false;
  label.userData.role = "label";

  const target = new Mesh(hit, new MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false, colorWrite: false }));
  target.scale.setScalar(Math.max(radius * 1.4, 6));
  target.userData.role = "hit";
  // Hidden by the type filter: not pointable either (the raycaster does not skip invisible groups).
  target.raycast = (raycaster: Raycaster, intersects: Intersection[]) => {
    if (group.visible) Mesh.prototype.raycast.call(target, raycaster, intersects);
  };

  for (const object of [core, halo, ...halos, done, badge, focus, label, ...(wire ? [wire] : [])]) noRaycast(object);
  group.add(halo, ...halos, core, ...(wire ? [wire] : []), done, badge, focus, label, target);

  return { id, kind, sun, radius, group, core, wire, glow: halo, halos, done, badge, focus, label, labelKey: "", labelSize: [0, 0], labelWanted: false, labelPriority: 3 };
}

/**
 * What an entry is drawn as when its real visual could not be built or styled (HoloScene catches
 * the failure): a plain slate sphere with a geometry and a material of its own - no shared
 * geometry, no textures, no shader, nothing that can fail the same way - so the entry stays in the
 * scene and can still be pointed at and opened, and every other entry is left as it is.
 */
export function createFallbackObject(id: string, radius: number): Group {
  const group = new Group();
  group.userData = { nodeId: id, kind: "fallback" };
  const size = Number.isFinite(radius) && radius > 0 ? radius : 10;
  const ball = new Mesh(new SphereGeometry(size, 16, 12), new MeshBasicMaterial({ color: SLATE, transparent: true, opacity: 0.85 }));
  ball.userData.role = "fallback";
  group.add(ball);
  return group;
}

function setOpacity(object: Sprite | LineSegments<BufferGeometry, LineBasicMaterial>, opacity: number) {
  object.visible = opacity > 0.004;
  object.material.opacity = opacity;
}

/**
 * Restyles one entry in place. `pixel` turns CSS pixels into the label sprite's units (it keeps
 * the same size on screen at any distance).
 */
export function styleVisual(visual: NodeVisual, style: VisualStyle, pixel: number): void {
  const { group, core, wire, glow, halos, done, badge, focus } = visual;
  group.visible = style.visible;
  if (!style.visible) return;
  const s = style.strength;
  const uniforms = core.material.uniforms;

  if (visual.sun) {
    uniforms.uCore.value.set(SUN_COLORS.core);
    uniforms.uRim.value.set(SUN_COLORS.body);
    uniforms.uPower.value = 1.6;
    uniforms.uOpacity.value = s;
    if (wire) {
      wire.material.color.set(SUN_COLORS.wire);
      setOpacity(wire, 0.45 * s);
    }
    glow.material.color.set(SUN_COLORS.body);
    setOpacity(glow, 0.85 * s);
    halos.forEach((ring, index) => {
      ring.material.color.set(index === 0 ? SUN_COLORS.body : SUN_COLORS.wire);
      setOpacity(ring, (index === 0 ? 0.55 : 0.26) * s);
    });
    setOpacity(done, 0);
    setOpacity(badge, 0);
  } else {
    const archived = style.state === "archived";
    const completed = style.state === "completed";
    const color = archived ? mixHex(style.color, SLATE, 0.72) : style.color;
    const planet = visual.kind === "planet";
    // Archived recedes to 25%; Done is calmer; Active is at full strength.
    const calm = archived ? 0.25 : completed ? 0.6 : 1;
    uniforms.uCore.value.set(mixHex(archived ? SLATE_DEEP : color, OBSIDIAN, planet ? 0.55 : 0.45));
    uniforms.uRim.value.set(color);
    uniforms.uPower.value = planet ? 2 : 1.2;
    uniforms.uOpacity.value = (planet ? 0.95 : 0.85) * calm * s;
    if (wire) {
      wire.material.color.set(archived ? SLATE : color);
      setOpacity(wire, (archived ? 0.25 : completed ? 0.42 : 0.72) * s);
    }
    glow.material.color.set(color);
    setOpacity(glow, archived ? 0 : (planet ? 0.5 : 0.26) * (completed ? 0.45 : 1) * s);
    done.material.color.set(DONE);
    setOpacity(done, completed ? 0.95 * s : 0);
    setOpacity(badge, completed ? s : 0);
  }

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
  visual.label.material.opacity = Math.max(s, 0.35);
}

const at = new Vector3();
const lens = new Vector3();

/**
 * Places every wanted label for this frame, like labels on a map: outward from the Sun on screen
 * (a planet on the right of the Sun is named to its right, one above it above), else under or
 * over its orb, never covering a label already placed. The Sun's, the open, pointed-at and lit
 * entries' and the planets' names always show (in their first spot if none is free); a moon's
 * name that has no free spot waits until the camera makes room.
 */
export function placeLabels(visuals: Iterable<NodeVisual>, camera: PerspectiveCamera, width: number, height: number, sun: Vector3 | null): void {
  const t = Math.tan((camera.fov * Math.PI) / 360);
  const centre = sun ? lens.copy(sun).project(camera) : null;
  const sunX = centre ? ((centre.x + 1) / 2) * width : width / 2;
  const sunY = centre ? ((1 - centre.y) / 2) * height : height / 2;
  const items: { visual: NodeVisual; x: number; y: number; r: number }[] = [];
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
    const r = ((visual.radius * (visual.sun ? 1.35 : 1.05)) / (distance * t)) * (height / 2);
    items.push({ visual, x: ((at.x + 1) / 2) * width, y: ((1 - at.y) / 2) * height, r });
  }
  items.sort((a, b) => a.visual.labelPriority - b.visual.labelPriority);
  const boxes: [number, number, number, number][] = [];
  for (const { visual, x, y, r } of items) {
    const [w, h] = visual.labelSize;
    const outward = visual.sun || Math.hypot(x - sunX, y - sunY) < 1 ? Math.PI / 2 : Math.atan2(y - sunY, x - sunX);
    const spots = [outward, Math.PI / 2, -Math.PI / 2].map((angle) => {
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      const ax = cos > 0.38 ? 1 : cos < -0.38 ? -1 : 0;
      const ay = sin > 0.38 ? 1 : sin < -0.38 ? -1 : 0;
      // The label's centre, in screen pixels from the orb's centre (y down).
      const ox = cos * (r + 4) + (ax * w) / 2;
      const oy = sin * (r + 4) + (ay * h) / 2;
      return { ox, oy, box: [x + ox, y + oy, w / 2, h / 2] as [number, number, number, number] };
    });
    const free = (box: [number, number, number, number]) =>
      !boxes.some(([bx, by, bw, bh]) => Math.abs(bx - box[0]) < bw + box[2] && Math.abs(by - box[1]) < bh + box[3]);
    let spot = spots.find((candidate) => free(candidate.box));
    if (!spot && visual.labelPriority <= 2) spot = spots[0];
    if (!spot) {
      visual.label.visible = false;
      continue;
    }
    boxes.push(spot.box);
    // The orb's centre sits at this point of the label (0..1 across, 0..1 up), so the label lands at the offset.
    visual.label.center.set(0.5 - spot.ox / w, 0.5 + spot.oy / h);
    visual.label.visible = true;
  }
}

/** The family font, read once from the page (a canvas cannot resolve the CSS variable). */
let fontFamily: string | null = null;
function family(): string {
  if (fontFamily === null) {
    const name = getComputedStyle(document.documentElement).getPropertyValue("--font-geist-sans").trim();
    fontFamily = name ? `${name}, Arial, sans-serif` : "Geist, Arial, sans-serif";
  }
  return fontFamily;
}

/** A crisp text label at twice its CSS size, on a dark halo; `width`/`height` are its CSS size. */
export function labelTexture(text: string, color: string, weight: number, px: number) {
  const scale = 2 * Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  const font = `${weight} ${px * scale}px ${family()}`;
  const [probe, measure] = canvas(4, 4);
  void probe;
  measure.font = font;
  const width = Math.ceil(measure.measureText(text).width / scale) + 8;
  const height = Math.ceil(px * 1.5);
  const [element, ctx] = canvas(width * scale, height * scale);
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.lineWidth = 4 * scale;
  ctx.strokeStyle = LABEL_HALO;
  ctx.strokeText(text, (width * scale) / 2, (height * scale) / 2);
  ctx.fillStyle = color;
  ctx.fillText(text, (width * scale) / 2, (height * scale) / 2);
  return { map: texture(element), width, height };
}

/** Frees one entry's own materials and label (the shared geometry and textures stay). */
export function disposeVisual(visual: NodeVisual): void {
  visual.group.traverse((object) => {
    const material = (object as Mesh).material as { dispose?: () => void; map?: { dispose: () => void } | null } | undefined;
    if (!material) return;
    if (object === visual.label) material.map?.dispose();
    material.dispose?.();
  });
}

/** Turns the Sun's and planets' wireframe globes (and the Sun's halos) a little; called once per frame. */
export function spin(visual: NodeVisual, step: number): void {
  if (visual.wire) visual.wire.rotation.y += step;
  visual.halos.forEach((ring, index) => {
    ring.material.rotation += (index === 0 ? 0.5 : -0.35) * step;
  });
}

// ---- the orbit rings ----------------------------------------------------------------------------------

/** One crisp ring per orbit on the orbital plane (blue at 22%); the outer orbit's ring is dashed. */
export function createRings(orbits: Orbits): Group {
  const rings = new Group();
  rings.userData.role = "orbits";
  orbits.radii.forEach((radius, index) => {
    if (index === 0) return;
    const points: number[] = [];
    for (let s = 0; s < 160; s++) {
      const a = (s / 160) * 2 * Math.PI;
      points.push(Math.cos(a) * radius, 0, Math.sin(a) * radius);
    }
    const geometry = new BufferGeometry();
    geometry.setAttribute("position", new Float32BufferAttribute(points, 3));
    const drifting = index === orbits.driftRing;
    const material = drifting
      ? new LineDashedMaterial({ color: 0x78a0ff, transparent: true, opacity: 0.16, dashSize: 5, gapSize: 9, depthWrite: false })
      : new LineBasicMaterial({ color: 0x78a0ff, transparent: true, opacity: 0.22, depthWrite: false });
    const loop = new LineLoop(geometry, material);
    if (drifting) loop.computeLineDistances();
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
