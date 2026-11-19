/**
 * The Memory page's model (app/jarvis/memory): Jarvis's long-term memory of the signed-in
 * person as a graph - entries (people, places, projects...) and the relations between them -
 * and the page's only door to the server, /api/memory/*. Pure: no React.
 *
 *   GET    /api/memory/graph            { nodes, links }
 *   PATCH  /api/memory/entities/<id>    { name?, type?, description? } -> { node, reembedded }
 *   DELETE /api/memory/entities/<id>    -> { deleted, links }   (its relations go with it)
 *   PATCH  /api/memory/relations/<id>   { status } -> { link }
 *   DELETE /api/memory/relations/<id>   -> { deleted }
 *
 * Every call answers `{ ok: true, data }` or `{ ok: false, status, message }` and never
 * throws; `message` is safe to show as it stands.
 */

export interface MemoryNode {
  id: string;
  name: string;
  /** One lowercase word or snake_case phrase, as the memory extractor wrote it ("person", "place"...). */
  type: string;
  description: string;
}

export interface MemoryLink {
  id: string;
  /** Node ids: the relation reads "source relation_type target" ("Nok sister_of me"). */
  source: string;
  target: string;
  relation_type: string;
  /** How many times the same fact was told (1 and up). */
  weight: number;
  /** active: holds now. completed: done (a finished project). archived: no longer true. */
  status: RelationStatus;
  /** When it was last told or changed (ISO 8601), or null from a brain that does not say. */
  updated_at: string | null;
}

export const RELATION_STATUSES = ["active", "completed", "archived"] as const;
export type RelationStatus = (typeof RELATION_STATUSES)[number];

/**
 * How the Memory graph draws a fact of each status (a line dash in screen pixels at any zoom,
 * and the legend's sample of it): a fact that ended stays drawn, as history.
 */
export const RELATION_DASH: Record<RelationStatus, number[] | null> = { active: null, completed: [6, 4], archived: [1.5, 3.5] };

export const RELATION_STATUS_LABEL: Record<RelationStatus, string> = {
  active: "Active",
  completed: "Completed",
  archived: "Archived",
};

/** The six types every entry has (the brain holds its extractor to them; the colours follow them). */
export const ENTITY_TYPES = ["person", "project", "technology", "organization", "place", "other"] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];

export interface MemoryGraph {
  nodes: MemoryNode[];
  links: MemoryLink[];
}

export interface MemoryEntityChanges {
  name?: string;
  type?: string;
  description?: string;
}

/** The limits the brain keeps to (mcp_memory.py); the form stops at them first. */
export const MEMORY_LIMITS = { name: 120, type: 40, description: 300 } as const;

// ---- the colour groups ---------------------------------------------------------------

/**
 * The legend: every entry's type falls in one of six groups, and the group gives its colour -
 * six neon hues for the Memory page's deep-space canvas (#060913). Checked as a set with the
 * dataviz validator, all pairs (any two planets can sit side by side): worst colour-blind
 * distance 9.2 (deutan, violet/sky), worst normal-vision 15.8 (pink/violet), every one at least
 * 3:1 on the canvas. They sit above the validator's dark lightness band (OKLCH L 0.70-0.82, not
 * 0.48-0.67) on purpose: that band is set for a #1a1a19 chart, and here they are lights in the
 * dark. Every node also carries its name (and the panel its type): colour is never the only sign.
 * Fixed per group, never by rank or count, so filtering never repaints anyone.
 */
export const TYPE_GROUPS = [
  { id: "person", label: "Person", color: "#34c8fd" },
  { id: "project", label: "Project", color: "#cb6cff" },
  { id: "technology", label: "Technology", color: "#ff53a8" },
  { id: "organization", label: "Organization", color: "#f87f02" },
  { id: "place", label: "Place", color: "#3fe691" },
  { id: "other", label: "Other", color: "#e3c327" },
] as const;

export type TypeGroupId = (typeof TYPE_GROUPS)[number]["id"];

const GROUP_BY_ID = new Map<string, (typeof TYPE_GROUPS)[number]>(TYPE_GROUPS.map((group) => [group.id, group]));

// The words the memory extractor (and people) use for each group.
const GROUP_WORDS: Record<Exclude<TypeGroupId, "other">, string[]> = {
  person: ["person", "people", "human", "family", "family_member", "friend", "colleague", "contact"],
  place: ["place", "city", "country", "location", "region", "province", "town", "village", "address", "venue", "landmark"],
  organization: ["organization", "organisation", "org", "company", "business", "school", "university", "institution", "team", "employer", "brand", "agency"],
  project: ["project", "product", "app", "goal", "initiative"],
  technology: ["technology", "tech", "software", "tool", "language", "programming_language", "framework", "library", "platform", "device", "service", "database", "api", "model"],
};
const GROUP_OF_WORD = new Map<string, TypeGroupId>(
  (Object.entries(GROUP_WORDS) as [TypeGroupId, string[]][]).flatMap(([group, words]) => words.map((word) => [word, group])),
);

/** The colour group a type belongs to ("City" -> place, "pet" -> other). */
export function groupOf(type: string): TypeGroupId {
  const word = type.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return GROUP_OF_WORD.get(word) ?? "other";
}

export function groupInfo(id: TypeGroupId) {
  return GROUP_BY_ID.get(id) ?? TYPE_GROUPS[TYPE_GROUPS.length - 1];
}

/** "sister_of" -> "sister of": how a relation reads. */
export function relationLabel(relationType: string): string {
  return relationType.replace(/_/g, " ").trim();
}

/** The person's own entry. */
export function isMe(node: Pick<MemoryNode, "name">): boolean {
  return node.name.trim().toLowerCase() === "me";
}

/** How an entry's name is shown: the person's own entry is "me (you)". */
export function displayName(node: Pick<MemoryNode, "name">): string {
  return isMe(node) ? "me (you)" : node.name;
}

/** Folded for search: case, accents and spacing do not count; Thai stays as it is. */
export function fold(text: string): string {
  return text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
}

/** The ids of the entries a search matches - by name first, then type and description. */
export function searchNodes(nodes: MemoryNode[], query: string): MemoryNode[] {
  const q = fold(query);
  if (!q) return [];
  const scored: { node: MemoryNode; score: number }[] = [];
  for (const node of nodes) {
    // As it is shown: the person's own entry is found by "me" and by "you".
    const name = fold(displayName(node));
    let score = -1;
    if (name === q || fold(node.name) === q) score = 0;
    else if (name.startsWith(q)) score = 1;
    else if (name.includes(q)) score = 2;
    else if (fold(node.type).includes(q)) score = 3;
    else if (fold(node.description).includes(q)) score = 4;
    if (score >= 0) scored.push({ node, score });
  }
  return scored
    .sort((a, b) => a.score - b.score || a.node.name.localeCompare(b.node.name))
    .map((entry) => entry.node);
}

// ---- the server ------------------------------------------------------------------------------

export type MemoryResult<T> = { ok: true; data: T } | { ok: false; status: number; message: string };

async function request<T>(url: string, init: RequestInit = {}): Promise<MemoryResult<T>> {
  let response: Response;
  try {
    response = await fetch(url, { cache: "no-store", ...init });
  } catch {
    return { ok: false, status: 0, message: "Couldn't reach the server. Check your connection and try again." };
  }
  const body: unknown = await response.json().catch(() => null);
  if (response.ok && body && typeof body === "object") return { ok: true, data: body as T };
  const error = body && typeof body === "object" && "error" in body ? (body as { error: unknown }).error : null;
  return {
    ok: false,
    status: response.status,
    message:
      typeof error === "string" && error
        ? error
        : response.status === 401
          ? "Sign in to see Jarvis's memory."
          : "Something went wrong. Please try again.",
  };
}

export function loadMemoryGraph(): Promise<MemoryResult<MemoryGraph>> {
  return request<MemoryGraph>("/api/memory/graph");
}

export function saveMemoryEntity(id: string, changes: MemoryEntityChanges) {
  return request<{ node: MemoryNode; reembedded: boolean }>(`/api/memory/entities/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export function removeMemoryEntity(id: string) {
  return request<{ deleted: string; links: string[] }>(`/api/memory/entities/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function saveMemoryRelationStatus(id: string, status: RelationStatus) {
  return request<{ link: MemoryLink }>(`/api/memory/relations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export function removeMemoryRelation(id: string) {
  return request<{ deleted: string }>(`/api/memory/relations/${encodeURIComponent(id)}`, { method: "DELETE" });
}
