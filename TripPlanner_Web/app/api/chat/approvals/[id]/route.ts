import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { decideApproval, JarvisBrainError } from "@/lib/jarvisBrain";
import { checkRateLimit } from "@/lib/rateLimit";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ id: string }> };

// A person answers a handful of cards a day; this only stops a script from walking ids.
const APPROVAL_RATE_LIMIT = 30;
const APPROVAL_RATE_WINDOW_MS = 60_000;

const ID = /^[A-Za-z0-9_-]{8,64}$/;

const DecisionSchema = z.object({ decision: z.enum(["allow", "deny"]) });

/**
 * POST /api/chat/approvals/<id> { decision: "allow" | "deny" } - the person's answer to an
 * Approval Card: a tool that changes something outside Jarvis (sends a LINE message, starts
 * a server, writes a new tool) is paused until they say yes (lib/jarvisBrain.ts, action_required).
 *
 * The answer goes to the brain with the SIGNED-IN user's id, never one the page supplies: the
 * brain only takes it from the person the card was sent to (and only the owner is ever asked),
 * so another signed-in user who learned an id cannot allow someone else's action.
 *
 * Responses: 200 { ok: true } the brain has it and carries on, 400 bad body, 401 signed out,
 * 403 not your request, 404 it is no longer waiting (answered, timed out, or the turn was
 * stopped), 429 too many, 502 the brain failed, 503 the brain cannot be reached.
 */
export async function POST(request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to answer Jarvis." }, { status: 401 });
  }

  const { id } = await params;
  const parsed = DecisionSchema.safeParse(await request.json().catch(() => null));
  if (!ID.test(id) || !parsed.success) {
    return NextResponse.json({ error: 'decision must be "allow" or "deny".' }, { status: 400 });
  }

  const rateLimit = checkRateLimit(`approval:${userId}`, APPROVAL_RATE_LIMIT, APPROVAL_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many answers - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    const outcome = await decideApproval(id, userId, parsed.data.decision);
    if (outcome === "gone") {
      return NextResponse.json({ error: "That request is no longer waiting for an answer." }, { status: 404 });
    }
    if (outcome === "forbidden") {
      return NextResponse.json({ error: "That request was not sent to you." }, { status: 403 });
    }
    return NextResponse.json({ ok: true });
  } catch (error) {
    if (!(error instanceof JarvisBrainError)) console.error("POST /api/chat/approvals failed:", error);
    const offline = error instanceof JarvisBrainError && /offline/i.test(error.message);
    return NextResponse.json(
      { error: error instanceof JarvisBrainError ? error.message : "Couldn't send your answer." },
      { status: offline ? 503 : 502 }
    );
  }
}
