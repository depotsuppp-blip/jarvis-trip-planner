/**
 * Google Places API (New) - Text Search and Nearby Search, used to ground
 * itinerary venue names in real data instead of letting the LLM invent
 * them - see app/api/trigger-jarvis/route.ts's two-stage generation
 * (Text Search) and app/api/trip/alternative/route.ts's live fallback
 * suggestion (Nearby Search). The swipe deck (searchPlacesForDeck /
 * fetchPlacePhotoUri, at the bottom) is the other consumer - see
 * app/api/places/deck/route.ts.
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

/**
 * The one place every Places API (New) call - Text Search, Nearby Search,
 * Place Photos - gets its key attached and its failures turned into a
 * PlacesApiError, so each caller below only deals with a success.
 */
async function placesFetch(
  url: string,
  init: {
    method: "GET" | "POST";
    headers?: Record<string, string>;
    body?: string;
    /**
     * Send the key as a ?key= parameter instead of the X-Goog-Api-Key
     * header. Only for Place Photos, whose docs show the key that way and
     * don't say the header is honored on that endpoint; every other call
     * here uses the header.
     */
    keyAsQueryParam?: boolean;
  }
): Promise<Response> {
  const apiKey = process.env.GOOGLE_MAPS_API_KEY;
  if (!apiKey) {
    throw new PlacesApiError("GOOGLE_MAPS_API_KEY is not configured.", "MISSING_API_KEY");
  }

  const { keyAsQueryParam, ...fetchInit } = init;
  const target = keyAsQueryParam
    ? `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(apiKey)}`
    : url;

  let response: Response;
  try {
    response = await fetch(target, {
      ...fetchInit,
      headers: keyAsQueryParam ? fetchInit.headers : { ...fetchInit.headers, "X-Goog-Api-Key": apiKey },
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

  return response;
}

async function postPlacesRequestRaw<T>(
  url: string,
  body: Record<string, unknown>,
  fieldMask: string
): Promise<T> {
  const response = await placesFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Goog-FieldMask": fieldMask },
    body: JSON.stringify(body),
  });
  return (await response.json().catch(() => ({}))) as T;
}

async function postPlacesRequest(url: string, body: Record<string, unknown>): Promise<PlaceResult[]> {
  const data = await postPlacesRequestRaw<PlacesResponse>(url, body, FIELD_MASK);
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

// ---------------------------------------------------------------------
// Swipe deck - app/api/places/deck and app/api/places/photo
//
// Google's Places policies forbid pre-fetching, caching or storing Places
// content other than place ids, and the Place Photos docs say outright
// that a photo name can't be cached and can expire. Nothing below caches
// anything: every deck is a fresh search, and a photo name is only ever
// resolved at the moment its image is shown.
// ---------------------------------------------------------------------

// A separate, richer mask than FIELD_MASK above on purpose: a swipe card
// needs a stable place id, a photo, and a human type label, none of which
// the itinerary prompt wants. Billing is unchanged by the extra fields -
// rating already puts this call at the Enterprise tier and everything
// else here is Pro, per Google's Text Search (New) field list (re-check
// that if rating/userRatingCount are ever dropped).
const DECK_FIELD_MASK = [
  "places.id",
  "places.displayName",
  "places.primaryTypeDisplayName",
  "places.shortFormattedAddress",
  "places.rating",
  "places.userRatingCount",
  "places.businessStatus",
  "places.photos",
].join(",");

export interface DeckSearchPlace {
  id: string;
  name: string;
  /** Google's label for the place's primary type, e.g. "Thai restaurant". */
  typeLabel: string | null;
  address: string | null;
  rating: number | null;
  ratingCount: number | null;
  /** e.g. "OPERATIONAL" / "CLOSED_PERMANENTLY"; null if Google omitted it. */
  businessStatus: string | null;
  /** "places/<id>/photos/<ref>" - resolve with fetchPlacePhotoUri, never store. */
  photoName: string | null;
  /** The first photo's author - Google requires showing it with the photo. */
  photoAttribution: { name: string; uri: string | null } | null;
}

interface DeckPlacesResponse {
  places?: {
    id?: string;
    displayName?: { text?: string };
    primaryTypeDisplayName?: { text?: string };
    shortFormattedAddress?: string;
    rating?: number;
    userRatingCount?: number;
    businessStatus?: string;
    photos?: {
      name?: string;
      authorAttributions?: { displayName?: string; uri?: string }[];
    }[];
  }[];
}

/**
 * One Text Search for the swipe deck. `query` is a plain-language seed such
 * as "quiet nature spots in Vancouver" (see DECK_CATEGORIES in
 * lib/placeDeck.ts). No place type is passed: the theme and city in the text
 * already say what to find, and a type would only bias the ranking - while a
 * strict one would hide results a traveler counts under the theme (a
 * waterfall is not a "park"). English labels are requested explicitly so the
 * cards read consistently whatever language a city's own listings use.
 *
 * Text Search bills per request, not per result, so asking for a bigger
 * page (pageSize up to 20) costs the same as asking for a few - a bigger
 * pool just gives lib/placeDeck.ts more to filter (closed, photo-less)
 * before dealing the final deck, at the price of a larger response.
 */
export async function searchPlacesForDeck(query: string, pageSize: number): Promise<DeckSearchPlace[]> {
  const data = await postPlacesRequestRaw<DeckPlacesResponse>(
    PLACES_TEXT_SEARCH_URL,
    { textQuery: query, pageSize, languageCode: "en" },
    DECK_FIELD_MASK
  );

  return (data.places ?? []).flatMap((place): DeckSearchPlace[] => {
    const name = place.displayName?.text;
    if (!place.id || !name) return [];

    const photo = place.photos?.find((p) => typeof p.name === "string" && p.name !== "");
    const author = photo?.authorAttributions?.find(
      (a) => typeof a.displayName === "string" && a.displayName !== ""
    );

    return [
      {
        id: place.id,
        name,
        typeLabel: place.primaryTypeDisplayName?.text ?? null,
        address: place.shortFormattedAddress ?? null,
        rating: typeof place.rating === "number" ? place.rating : null,
        ratingCount: typeof place.userRatingCount === "number" ? place.userRatingCount : null,
        businessStatus: place.businessStatus ?? null,
        photoName: photo?.name ?? null,
        photoAttribution: author?.displayName
          ? { name: author.displayName, uri: author.uri ?? null }
          : null,
      },
    ];
  });
}

/**
 * The only shape of photo name this app will ever interpolate into a
 * Places URL - checked both by GET /api/places/photo (to answer 400) and
 * again inside fetchPlacePhotoUri, so a stray caller can't turn that route
 * into "call any Places endpoint with the server's key".
 */
export const PLACE_PHOTO_NAME_RE = /^places\/[A-Za-z0-9_-]{1,300}\/photos\/[A-Za-z0-9_-]{1,1000}$/;

/**
 * Resolves a photo name to a direct image URL. skipHttpRedirect=true makes
 * Google answer with JSON ({ photoUri }) instead of a 302 to the image, so
 * the API key travels only on this one server-to-Google request (as the
 * ?key= parameter Google's docs show for this endpoint) - the returned
 * photoUri is keyless, which is what lets the browser fetch the image
 * straight from Google without the key ever reaching it. Google describes
 * the URI as short-lived: callers must use it immediately (the photo route
 * redirects to it) and never store it.
 */
export async function fetchPlacePhotoUri(photoName: string, maxWidthPx: number): Promise<string> {
  if (!PLACE_PHOTO_NAME_RE.test(photoName)) {
    throw new PlacesApiError("Invalid photo name.", "INVALID_PHOTO_NAME");
  }

  const response = await placesFetch(
    `https://places.googleapis.com/v1/${photoName}/media?maxWidthPx=${maxWidthPx}&skipHttpRedirect=true`,
    { method: "GET", keyAsQueryParam: true }
  );
  const data: { photoUri?: unknown } | null = await response.json().catch(() => null);

  if (typeof data?.photoUri !== "string" || !data.photoUri.startsWith("https://")) {
    throw new PlacesApiError("The Places API returned no photo URI.", "NO_PHOTO_URI");
  }
  return data.photoUri;
}
