/**
 * Authentication for SERVER-TO-SERVER routes: the ones only Jarvis's Python
 * side (api_server.py / plugins/trip_planner.py) calls, with no browser session
 * behind them - today POST /api/trip/create.
 *
 * Same shared secret and headers as the older HMAC routes (X-Ts, X-Sig; the
 * secret is API_SECRET_KEY here and TRIP_API_SECRET_KEY in the Python .env -
 * two names, one value; see app/api/poll/[id]/admin-token/route.ts), with one
 * difference: the signature also covers a digest of the REQUEST BODY.
 *
 *     sig = HMAC-SHA256(secret, "{ts}.{METHOD}.{path}.{sha256(body) as hex}")
 *
 * The older routes sign only "{ts}.{METHOD}.{path}", which is enough when the
 * body is not what carries the authority. Here it is: /api/trip/create takes a
 * userId in the body and spends paid quota on that person's behalf, so a captured
 * (ts, sig) pair must not be replayable within the 5-minute window with a
 * different userId or destination. Binding the body closes that.
 *
 * plugins/trip_planner.py's _sign_request builds the identical string - the two
 * must stay byte-for-byte equal, and tests on both sides pin the same vector.
 */

import crypto from "crypto";

// How far a request's X-Ts may drift from "now" before it is rejected - bounds
// the window a captured (ts, sig) pair could be replayed in.
const MAX_TIMESTAMP_SKEW_SECONDS = 300;

const HEX_SHA256_RE = /^[0-9a-f]{64}$/i;

export interface SignatureFailure {
  /** 401 for a bad or missing signature, 503 when this server has no secret at all. */
  status: 401 | 503;
  error: string;
}

/** SHA-256 hex digest of the raw request body, as signed. */
export function bodyDigest(rawBody: string): string {
  return crypto.createHash("sha256").update(rawBody, "utf8").digest("hex");
}

/** The signature a caller holding `secret` sends for this exact request. */
export function signRequest(secret: string, ts: string, method: string, path: string, rawBody: string): string {
  return crypto.createHmac("sha256", secret).update(`${ts}.${method}.${path}.${bodyDigest(rawBody)}`).digest("hex");
}

/**
 * Checks a request's X-Ts/X-Sig against API_SECRET_KEY and the body that was
 * actually received (`rawBody` - the text, read before any JSON parsing, so the
 * digest is of the bytes the caller signed). Returns null when the request is
 * authentic, otherwise why not.
 *
 * Unlike the older routes' optional check, a request with no signature is
 * rejected: there is no anonymous caller of a server-to-server route.
 * Comparison is constant-time.
 */
export function verifySignedRequest(
  headers: Headers,
  method: string,
  path: string,
  rawBody: string
): SignatureFailure | null {
  const secret = process.env.API_SECRET_KEY;
  if (!secret) {
    return { status: 503, error: "API_SECRET_KEY is not configured on the Trip Planner server." };
  }

  const ts = headers.get("x-ts");
  const sig = headers.get("x-sig");
  if (!ts || !sig) {
    return { status: 401, error: "Missing X-Ts/X-Sig headers." };
  }

  const tsSeconds = Number(ts);
  if (!Number.isFinite(tsSeconds)) {
    return { status: 401, error: "Invalid X-Ts header." };
  }
  if (Math.abs(Date.now() / 1000 - tsSeconds) > MAX_TIMESTAMP_SKEW_SECONDS) {
    return { status: 401, error: "Request timestamp is too old." };
  }

  if (!HEX_SHA256_RE.test(sig)) {
    return { status: 401, error: "Invalid X-Sig header." };
  }

  const expected = Buffer.from(signRequest(secret, ts, method, path, rawBody), "hex");
  const provided = Buffer.from(sig, "hex");
  if (expected.length !== provided.length || !crypto.timingSafeEqual(expected, provided)) {
    return { status: 401, error: "Signature mismatch." };
  }

  return null;
}
