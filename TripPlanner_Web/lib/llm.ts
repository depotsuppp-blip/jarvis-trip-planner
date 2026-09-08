/**
 * Shared Anthropic call configuration for every structured-output call
 * this app makes - app/api/trigger-jarvis/route.ts's Stage 1/Stage 2, and
 * app/api/trip/alternative/route.ts's real-time swap. One place to change
 * the model or token budget rather than two call sites drifting apart.
 *
 * Cost-optimization default: claude-haiku-4-5, not opus-5/sonnet-5 -
 * already decided against escalating once the grounded (Places-backed)
 * architecture replaced the single-call approach. No thinking/effort
 * config anywhere these are used: unlike Opus 5/Sonnet 5/Fable 5, Haiku
 * 4.5 is pre-4.6-tier - output_config.effort errors outright on this
 * model, and omitting `thinking` entirely (rather than an explicit
 * {type:"disabled"}) is its correct "no extended thinking" state.
 */
export const FINALIZE_MODEL = "claude-haiku-4-5-20251001";
// Raised from the original 4096: trip length is now dynamic (up to
// MAX_TRIP_DAYS=14, see lib/tripDates.ts) and Stage 1's prompt requires
// 4-6 stops per day, not ~5-6 total - a long, dense trip's skeleton or
// final-write JSON can genuinely need more than 4096 tokens, and a
// truncated structured-output response fails messages.parse outright
// rather than degrading gracefully. This only raises the CEILING;
// Anthropic bills actual completion length, so a short response (a
// single-stop alternative choice, a 1-day skeleton) costs the same as
// before.
export const FINALIZE_MAX_TOKENS = 8192;
