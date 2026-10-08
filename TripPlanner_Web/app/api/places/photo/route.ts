import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { PLACE_PHOTO_NAME_RE, PlacesApiError, fetchPlacePhotoUri } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";

// A deck shows at most ~20 photos, loaded a few at a time as cards come up;
// this leaves room for a few reloads and "start over"s a minute while still
// stopping a runaway loop from running up Place Photos charges.
const PHOTO_RATE_LIMIT = 120;
const PHOTO_RATE_WINDOW_MS = 60_000;

const DEFAULT_WIDTH_PX = 800;
const MIN_WIDTH_PX = 100;
const MAX_WIDTH_PX = 1600;

// Missing, empty or non-numeric all mean "not given" (the default), never
// "as small as allowed".
function clampWidth(raw: string | null): number {
  const parsed = raw?.trim() ? Number(raw) : NaN;
  if (!Number.isFinite(parsed)) return DEFAULT_WIDTH_PX;
  return Math.min(Math.max(Math.round(parsed), MIN_WIDTH_PX), MAX_WIDTH_PX);
}

/**
 * GET /api/places/photo?name=places/<id>/photos/<ref>&w=800 - what a
 * swipe card's <img> loads (see lib/placeDeck.ts's photoProxyUrl).
 *
 * Exists so the server's GOOGLE_MAPS_API_KEY never appears in a URL the
 * browser sees: Google's own photo URLs embed the key, and anyone could
 * lift it from the page and bill requests to this project. Here the key
 * stays on the server-to-Google call that resolves the photo name to an
 * image URL (see fetchPlacePhotoUri), and the browser is then 302'd to
 * that keyless Google-hosted image - so no image bytes pass through this
 * server either.
 *
 * Signed-in callers only and rate-limited per user, since every call is a
 * billable Place Photos request. `name` must be exactly a Places photo
 * resource name (PLACE_PHOTO_NAME_RE) - this is not a general "call any
 * Places endpoint" proxy. The redirect is cacheable by the browser for a
 * few minutes only: Google calls these image URLs short-lived, and says a
 * photo name itself can expire, so neither is kept longer than a session.
 */
export async function GET(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see place photos." }, { status: 401 });
  }

  const name = request.nextUrl.searchParams.get("name") ?? "";
  if (!PLACE_PHOTO_NAME_RE.test(name)) {
    return NextResponse.json({ error: "name must be a Places photo resource name." }, { status: 400 });
  }
  const width = clampWidth(request.nextUrl.searchParams.get("w"));

  const rateLimit = checkRateLimit(`photo:${userId}`, PHOTO_RATE_LIMIT, PHOTO_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many photo requests - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    const photoUri = await fetchPlacePhotoUri(name, width);
    return new NextResponse(null, {
      status: 302,
      headers: { Location: photoUri, "Cache-Control": "private, max-age=300" },
    });
  } catch (error) {
    if (error instanceof PlacesApiError) {
      console.error(`GET /api/places/photo: Places API error (${error.reason}):`, error.message);
      // A photo name Google no longer recognizes (expired, or never valid)
      // is a 404 for this one image; anything else is Google or our
      // config failing, which is a 502.
      const notFound = error.reason === "HTTP_404" || error.reason === "HTTP_400";
      return NextResponse.json(
        { error: "That photo isn't available.", reason: error.reason },
        { status: notFound ? 404 : 502 }
      );
    }
    console.error("GET /api/places/photo failed:", error);
    return NextResponse.json({ error: "Something went wrong loading that photo." }, { status: 500 });
  }
}
