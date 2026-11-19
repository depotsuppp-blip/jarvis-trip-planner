import { z } from "zod";
import type { SystemTopology } from "@/lib/jarvis/system";

/**
 * The topology door: the Python brain's GET /system/topology (api_server.py, system_topology.py) -
 * what Jarvis can do right now, which agent or tool server does it, and whether it works. The
 * System Topology page (/jarvis/system) reaches it only through this app's /api/system/topology,
 * which adds the signed-in person.
 *
 *   GET {JARVIS_SYSTEM_URL}/topology   header X-Jarvis-User-Id: <User.id>
 *       -> { generated_at, viewer: { owner, hidden }, orchestrator, providers: [...],
 *            telemetry: { status, active_providers, total_providers, total_capabilities } }
 *       errors { code, detail }: 401 no_user, 503 unavailable, 500 server
 *
 * JARVIS_SYSTEM_URL defaults to JARVIS_BRAIN_URL with its /chat swapped for /system.
 *
 * WHAT LEAVES THIS FILE. Only the fields below, each cut to a length, and every free-text field
 * scrubbed once more of anything shaped like a path, a connection string, a key or an e-mail
 * address (the brain writes its own sentences and should never send one - this is the second
 * lock). The brain's error `detail` never leaves: the page gets a fixed sentence per failure.
 */

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
const SYSTEM_URL = (process.env.JARVIS_SYSTEM_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/system")).replace(/\/$/, "");
const TIMEOUT_MS = 10_000;

export const SYSTEM_SIGNED_OUT = "Sign in to see Jarvis's systems.";

export type SystemFailureCode = "offline" | "unavailable";

export const SYSTEM_MESSAGES: Record<SystemFailureCode, string> = {
  offline: "Jarvis isn't running right now, so its systems can't be shown.",
  unavailable: "Jarvis couldn't report on its systems just now. Try again in a moment.",
};

export class JarvisSystemError extends Error {
  readonly code: SystemFailureCode;

  constructor(code: SystemFailureCode) {
    super(SYSTEM_MESSAGES[code]);
    this.code = code;
  }
}

// ---- the second lock: nothing that looks like a secret or a place on disk -----------------

const HIDDEN = "[hidden]";
const SCRUB: RegExp[] = [
  /\b[a-z][a-z0-9+.-]*:\/\/[^\s/@]+:[^\s@]+@\S+/gi, // URLs with credentials (postgresql://user:pass@host)
  /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis):\/\/\S+/gi, // any database URL
  /\b[A-Za-z]:\\[^\s"'`]+/g, // Windows paths
  /(?:^|\s)\/(?:home|Users|root|var|etc|opt|tmp|mnt|srv)\/\S+/g, // POSIX paths
  /\bsk-[A-Za-z0-9_-]{12,}/g, // OpenAI / Anthropic style keys
  /\bAIza[0-9A-Za-z_-]{20,}/g, // Google API keys
  /\bgsk_[A-Za-z0-9]{20,}/g, // Groq keys
  /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}/g, // JWTs
  /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, // e-mail addresses
];

export function scrub(text: string): string {
  return SCRUB.reduce((value, pattern) => value.replace(pattern, (match) => (/^\s/.test(match) ? ` ${HIDDEN}` : HIDDEN)), text);
}

const text = (max: number) => z.string().max(max * 4).transform((value) => scrub(value).slice(0, max));

const Status = z.enum(["online", "auth_required", "offline"]);
const Kind = z.enum(["orchestrator", "agent", "mcp_server", "core"]);
const Category = z.enum(["orchestrator", "agent", "mcp", "data", "core"]);
const Access = z.enum(["API", "MCP", "Internal"]);
const Id = z.string().min(1).max(120).regex(/^[\w:.-]+$/);

const CapabilitySchema = z.object({
  id: Id,
  name: text(120),
  label: text(80),
  description: text(240),
  needs_approval: z.boolean(),
  status: Status,
  note: text(200),
});

const RouteSchema = z.object({
  role: text(30),
  vendor: text(40),
  model: text(80),
  state: text(30).catch(""),
  error: text(60).optional(),
});

const ProviderSchema = z.object({
  id: Id,
  name: text(60),
  title: text(80),
  kind: Kind,
  category: Category,
  vendor: text(40),
  model: text(80).nullable(),
  access: Access,
  status: Status,
  status_note: text(40),
  diagnostics: z.array(text(400)).max(12),
  // Google scope URLs only: anything else is dropped.
  missing_scopes: z
    .array(z.string().max(200))
    .max(12)
    .transform((scopes) => scopes.filter((scope) => /^https:\/\/www\.googleapis\.com\/auth\/[\w.-]+$/.test(scope))),
  owner_only: z.boolean(),
  description: text(300),
  routes: z.array(RouteSchema).max(8),
  uses: z.array(Id).max(12),
  capabilities: z.array(CapabilitySchema).max(80),
  workflows: z.array(z.object({ label: text(60), prompt: text(600) })).max(6),
});

const TopologySchema = z.object({
  generated_at: z.string().max(40),
  viewer: z.object({ owner: z.boolean(), hidden: z.number().int().min(0).max(100) }),
  orchestrator: ProviderSchema,
  providers: z.array(ProviderSchema).max(60),
  telemetry: z.object({
    status: z.enum(["optimal", "degraded"]),
    active_providers: z.number().int().min(0),
    total_providers: z.number().int().min(0),
    total_capabilities: z.number().int().min(0),
  }),
});

/** The topology as the signed-in person sees it (the owner sees the owner-only parts too). */
export async function fetchSystemTopology(userId: string): Promise<SystemTopology> {
  let response: Response;
  try {
    response = await fetch(`${SYSTEM_URL}/topology`, {
      headers: { "X-Jarvis-User-Id": userId },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // Not running, not reachable from here (a Vercel deployment), or too slow.
    throw new JarvisSystemError("offline");
  }
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    // A brain from before this endpoint answers 404; anything else is the brain's own failure.
    console.error(`Jarvis system topology answered ${response.status}`);
    throw new JarvisSystemError(response.status === 404 ? "offline" : "unavailable");
  }
  const parsed = TopologySchema.safeParse(payload);
  if (!parsed.success) {
    console.error("Jarvis system topology sent an unexpected body");
    throw new JarvisSystemError("unavailable");
  }
  return parsed.data;
}
