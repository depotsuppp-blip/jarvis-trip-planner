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
export const FINALIZE_MAX_TOKENS = 4096;
