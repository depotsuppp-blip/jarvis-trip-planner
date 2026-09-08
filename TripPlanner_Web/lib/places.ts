/**
 * Google Places API (New) - Text Search and Nearby Search, used to ground
 * itinerary venue names in real data instead of letting the LLM invent
 * them - see app/api/trigger-jarvis/route.ts's two-stage generation
 * (Text Search) and app/api/trip/alternative/route.ts's live fallback
 * suggestion (Nearby Search).
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
  /**
   * Google's own PRICE_LEVEL_* enum string (e.g. "PRICE_LEVEL_MODERATE"),
   * or null if Google didn't return one for this place - common for
   * venues that don't take payment, or sparse data. Passed to the Stage 2
   * cost-estimating LLM call (formatSlotForPrompt in
   * app/api/trigger-jarvis/route.ts) as a real signal instead of a guess.
   */
  priceLevel: string | null;
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

const PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText";
const PLACES_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby";

// Minimal on purpose - a broader field mask (photos, reviews, opening
// hours, ...) bills at a higher Places API SKU tier, and none of that is
// used by the itinerary prompt this feeds. Shared by both Text Search and
// Nearby Search below - both return the same Place resource shape.
//
// places.location is included for Stage 2.5's travel-time enrichment
// (app/api/trigger-jarvis/route.ts) - verified against Google's Place
// Data Fields (New) table before adding it: for Text Search specifically,
// displayName/formattedAddress/location are all "Pro" tier, while rating
// alone already puts this call at "Enterprise" tier (Google bills at the
// HIGHEST SKU touched by any field in the mask, per Places API (New)'s
// usage-and-billing docs). Since rating was already here, adding location
// does not raise the SKU or the price - Enterprise already dominates Pro.
// priceLevel is also documented as "Pro" tier, so the same reasoning
// applies to it. Worth re-confirming against Google's current pricing
// docs if rating is ever removed from this mask, since Pro would then
// become the billed tier again (still includes location/priceLevel, just
// at a different price).
const FIELD_MASK =
  "places.displayName,places.formattedAddress,places.rating,places.location,places.priceLevel";

interface PlacesResponse {
  places?: {
    displayName?: { text?: string };
    formattedAddress?: string;
    rating?: number;
    location?: { latitude?: number; longitude?: number };
    priceLevel?: string;
  }[];
}

interface GoogleErrorBody {
  error?: {
    message?: string;
    details?: { reason?: string }[];
  };
}

function mapPlace(place: NonNullable<PlacesResponse["places"]>[number]): PlaceResult {
  return {
    name: place.displayName?.text ?? "Unknown",
    address: place.formattedAddress ?? "",
    rating: typeof place.rating === "number" ? place.rating : null,
    location:
      typeof place.location?.latitude === "number" && typeof place.location?.longitude === "number"
        ? { lat: place.location.latitude, lng: place.location.longitude }
        : null,
    priceLevel:
      typeof place.priceLevel === "string" && place.priceLevel !== "PRICE_LEVEL_UNSPECIFIED"
        ? place.priceLevel
        : null,
  };
}

async function postPlacesRequest(url: string, body: Record<string, unknown>): Promise<PlaceResult[]> {
  const apiKey = process.env.GOOGLE_MAPS_API_KEY;
  if (!apiKey) {
    throw new PlacesApiError("GOOGLE_MAPS_API_KEY is not configured.", "MISSING_API_KEY");
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": apiKey,
        "X-Goog-FieldMask": FIELD_MASK,
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new PlacesApiError(
      `Failed to reach the Places API: ${err instanceof Error ? err.message : String(err)}`,
      "NETWORK_ERROR"
    );
  }

  if (!response.ok) {
    const errBody: GoogleErrorBody | null = await response.json().catch(() => null);
    const reason =
      errBody?.error?.details?.find((d) => typeof d.reason === "string")?.reason ??
      `HTTP_${response.status}`;
    const message =
      typeof errBody?.error?.message === "string"
        ? errBody.error.message
        : `Places API request failed with HTTP ${response.status}.`;
    throw new PlacesApiError(message, reason);
  }

  const data: PlacesResponse = await response.json().catch(() => ({}));
  return (data.places ?? []).map(mapPlace);
}

export async function searchPlacesText(query: string): Promise<PlaceResult[]> {
  return postPlacesRequest(PLACES_TEXT_SEARCH_URL, { textQuery: query });
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

/**
 * "What's actually near this exact point right now" - used by POST
 * /api/trip/alternative for a real-time fallback suggestion (a stop that
 * turned out closed, ran out of time, ...), as opposed to
 * searchPlacesForSlot's category+neighborhood-name matching used when the
 * original itinerary was generated. `includedType` must be one of
 * Google's own Place Types (New) values (e.g. "cafe", "restaurant") - not
 * validated here; an invalid one surfaces as a PlacesApiError from
 * Google itself, same as any other Places API failure this module
 * throws.
 */
export async function searchNearbyPlaces(
  location: LatLng,
  radiusMeters: number,
  includedType: string,
  maxResultCount = 3
): Promise<PlaceResult[]> {
  return postPlacesRequest(PLACES_NEARBY_SEARCH_URL, {
    includedTypes: [includedType],
    maxResultCount,
    locationRestriction: {
      circle: {
        center: { latitude: location.lat, longitude: location.lng },
        radius: radiusMeters,
      },
    },
  });
}

/**
 * Text Search, biased toward (not restricted to) a point - used by POST
 * /api/trip/alternative when the traveler typed a SPECIFIC venue name
 * rather than a category (see that route's classifyPlaceQuery): Nearby
 * Search's includedTypes only accepts Google's fixed Place Types (New)
 * enum, which a proper noun like "Katsuya" was never going to match, so
 * this reuses ordinary Text Search instead with locationBias standing in
 * for Nearby Search's locationRestriction - a bias, not a hard filter,
 * which is correct for a named search: the exact branch closest to the
 * traveler should still win over a same-named venue across town, without
 * hiding it outright if it's the only real match.
 */
export async function searchPlacesTextNear(
  query: string,
  location: LatLng,
  radiusMeters: number
): Promise<PlaceResult[]> {
  return postPlacesRequest(PLACES_TEXT_SEARCH_URL, {
    textQuery: query,
    locationBias: {
      circle: {
        center: { latitude: location.lat, longitude: location.lng },
        radius: radiusMeters,
      },
    },
  });
}
