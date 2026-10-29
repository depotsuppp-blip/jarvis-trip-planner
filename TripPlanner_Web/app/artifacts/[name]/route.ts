import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { artifactMime, artifactPath } from "@/lib/jarvis/artifacts";
import { fetchArtifact, JarvisBrainError } from "@/lib/jarvisBrain";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";

// A chat page loads each picture once (the browser keeps it after that); this leaves
// room for long sessions full of them while stopping a script that walks file names.
const ARTIFACT_RATE_LIMIT = 120;
const ARTIFACT_RATE_WINDOW_MS = 60_000;
// The brain refuses to save anything bigger (agents/creative_agent.py, MAX_IMAGE_BYTES).
const MAX_ARTIFACT_BYTES = 50 * 1024 * 1024;

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ name: string }> };

function notFound() {
  return NextResponse.json({ error: "That image isn't available." }, { status: 404 });
}

/**
 * GET /artifacts/<name> - a picture Jarvis made, for the <img> in a chat reply
 * (`![Generated Image](/artifacts/image_YYYYMMDD_HHMMSS.png)`, see lib/jarvis/artifacts.ts).
 *
 * The file lives in the Jarvis project's artifacts/ folder, next to the brain, and the
 * browser never talks to the brain: this route fetches it from the brain's own
 * GET /artifacts/<name> (lib/jarvisBrain.ts, fetchArtifact) and passes it on.
 *
 * Only to the person whose chat shows it. The names are timestamps and easy to guess,
 * so being signed in is not enough: one of this person's own conversations must hold a
 * reply from Jarvis with exactly this picture in it. Anyone else - another signed-in
 * user guessing names, a picture made by voice that no chat shows - gets the same 404 as
 * a file that does not exist. `name` must be a name Jarvis gives a picture, so this is
 * never a way to read anything else the brain serves.
 *
 * Responses: 200 the image (cached privately for good: a file is never overwritten),
 * 401 signed out, 404 not theirs / no such image, 429 too many, 500 database failed,
 * 502 the brain sent something that is not that image, 503 the brain cannot be reached
 * (it is off, or this deployment - Vercel - has no brain beside it).
 */
export async function GET(_request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see this image." }, { status: 401 });
  }

  const { name } = await params;
  const mime = artifactMime(name);
  if (!mime) return notFound();

  const rateLimit = checkRateLimit(`artifact:${userId}`, ARTIFACT_RATE_LIMIT, ARTIFACT_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many image requests - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    const shown = await prisma.message.findFirst({
      where: { role: "jarvis", content: { contains: `](${artifactPath(name)})` }, conversation: { userId } },
      select: { id: true },
    });
    if (!shown) return notFound();
  } catch (error) {
    console.error("GET /artifacts failed checking whose image it is:", error);
    return NextResponse.json({ error: "Something went wrong loading that image." }, { status: 500 });
  }

  let upstream: Response;
  try {
    upstream = await fetchArtifact(name);
  } catch (error) {
    const message = error instanceof JarvisBrainError ? error.message : "Jarvis brain is offline.";
    return NextResponse.json({ error: message }, { status: 503 });
  }

  if (upstream.status === 404) return notFound();
  const type = (upstream.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
  const declared = Number(upstream.headers.get("content-length"));
  if (!upstream.ok || !upstream.body || type !== mime || (Number.isFinite(declared) && declared > MAX_ARTIFACT_BYTES)) {
    console.error(`GET /artifacts/${name}: the brain answered ${upstream.status} with ${type || "no type"}.`);
    await upstream.body?.cancel().catch(() => {});
    return NextResponse.json({ error: "Jarvis sent something that isn't that image." }, { status: 502 });
  }

  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": mime,
      ...(Number.isFinite(declared) && declared > 0 ? { "Content-Length": String(declared) } : {}),
      "Cache-Control": "private, max-age=31536000, immutable",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
