/**
 * Google Places API (New) - Text Search, used to ground itinerary venue
 * names in real data instead of letting the LLM invent them - see
 * app/api/trigger-jarvis/route.ts's two-stage generation.
 *
 * This is a DIFFERENT product from the legacy Places API
 * (maps.googleapis.com/maps/api/place/textsearch/json) that
 * plugins/trip_planner.py's _fetch_places_text_search already uses -
 * "Places API (New)" must be separately enabled on the Google Cloud
 * project even though GOOGLE_MAPS_API_KEY is the same key for both.
 */

export interface LatLng {
  lat: number;
  lng: number;
}

export interface PlaceResult {
  name: string;
  address: string;
  rating: number | null;
  /**
   * null only if Google's response omits it for this specific place (rare
   * for Text Search) - never omitted from the field mask itself, so this
   * is a per-place data gap, not a config issue. Stage 2.5 (see
   * app/api/trigger-jarvis/route.ts) treats a null location as "no travel
   * time possible to/from this stop" rather than failing.
   */
  location: LatLng | null;
}

/**
 * Thrown only for a genuine API-level failure (missing/invalid key, the
 * API not enabled, billing not active, quota exhausted, ...) - never for
 * a normal "zero results" search, which is a successful empty array a
 * caller should broaden/retry or accept, not an error. `reason` is
 * Google's own machine-readable error reason (e.g. "SERVICE_DISABLED",
 * "API_KEY_INVALID") when present, so a caller can report specifically
 * which Google Cloud setting is the problem instead of a generic 500 -
 * exactly the diagnosis that has been a recurring pain point in this
 * project (see this route's LINE_CHANNEL_ID debugging history).
 */
export class PlacesApiError extends Error {
  constructor(
    message: string,
    public readonly reason: string
  ) {
    super(message);
    this.name = "PlacesApiError";
  }
}

const PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText";

// Minimal on purpose - a broader field mask (photos, reviews, opening
// hours, ...) bills at a higher Places API SKU tier, and none of that is
// used by the itinerary prompt this feeds.
//
// places.location is included for Stage 2.5's travel-time enrichment
// (app/api/trigger-jarvis/route.ts) - verified against Google's Place
// Data Fields (New) table before adding it: for Text Search specifically,
// displayName/formattedAddress/location are all "Pro" tier, while rating
// alone already puts this call at "Enterprise" tier (Google bills at the
// HIGHEST SKU touched by any field in the mask, per Places API (New)'s
// usage-and-billing docs). Since rating was already here, adding location
// does not raise the SKU or the price - Enterprise already dominates Pro.
// This is worth re-confirming against Google's current pricing docs if
// rating is ever removed from this mask, since Pro would then become the
// billed tier again (still includes location, just at a different price).
const FIELD_MASK =
  "places.displayName,places.formattedAddress,places.rating,places.location";

interface PlacesSearchTextResponse {
  places?: {
    displayName?: { text?: string };
    formattedAddress?: string;
    rating?: number;
    location?: { latitude?: number; longitude?: number };
  }[];
}

interface GoogleErrorBody {
  error?: {
    message?: string;
    details?: { reason?: string }[];
  };
}

export async function searchPlacesText(query: string): Promise<PlaceResult[]> {
  const apiKey = process.env.GOOGLE_MAPS_API_KEY;
  if (!apiKey) {
    throw new PlacesApiError("GOOGLE_MAPS_API_KEY is not configured.", "MISSING_API_KEY");
  }

  let response: Response;
  try {
    response = await fetch(PLACES_SEARCH_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": apiKey,
        "X-Goog-FieldMask": FIELD_MASK,
      },
      body: JSON.stringify({ textQuery: query }),
    });
  } catch (err) {
    throw new PlacesApiError(
      `Failed to reach the Places API: ${err instanceof Error ? err.message : String(err)}`,
      "NETWORK_ERROR"
    );
  }

  if (!response.ok) {
    const body: GoogleErrorBody | null = await response.json().catch(() => null);
    const reason =
      body?.error?.details?.find((d) => typeof d.reason === "string")?.reason ??
      `HTTP_${response.status}`;
    const message =
      typeof body?.error?.message === "string"
        ? body.error.message
        : `Places API request failed with HTTP ${response.status}.`;
    throw new PlacesApiError(message, reason);
  }

  const data: PlacesSearchTextResponse = await response.json().catch(() => ({}));
  return (data.places ?? []).map((place) => ({
    name: place.displayName?.text ?? "Unknown",
    address: place.formattedAddress ?? "",
    rating: typeof place.rating === "number" ? place.rating : null,
    location:
      typeof place.location?.latitude === "number" && typeof place.location?.longitude === "number"
        ? { lat: place.location.latitude, lng: place.location.longitude }
        : null,
  }));
}

/**
 * A category+area search, broadened once (drop the area, keep the
 * destination) if the first attempt returns zero results - a real trip
 * area name ("Nimman") is sometimes too narrow for Text Search to match
 * against, and this costs one extra call only in that case, not on
 * every slot. Still returns [] (never throws) for a genuine "nothing
 * found even broadened" - see this module's docstring on PlacesApiError
 * for why that's not an error condition.
 */
export async function searchPlacesForSlot(
  category: string,
  area: string,
  destination: string
): Promise<PlaceResult[]> {
  const primary = await searchPlacesText(`${category} near ${area}, ${destination}`);
  if (primary.length > 0) {
    return primary;
  }
  return searchPlacesText(`${category} in ${destination}`);
}
