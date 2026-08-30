/**
 * Shared LIFF SDK singleton. liff.init() must run exactly once per page
 * load - calling it twice with the LINE in-app browser's real session
 * cookies can throw - but both the root layout (see
 * components/LiffInitializer.tsx, which handles the liff.state redirect
 * on first load) and any page that needs to check login state (e.g.
 * app/trip/poll/[id]/page.tsx) need to know once it's done. Memoizing
 * the in-flight promise here means every caller awaits the SAME init
 * call instead of racing to start their own.
 */

import liff from "@line/liff";

let initPromise: Promise<boolean> | null = null;

/**
 * Resolves true once liff.init() has succeeded, false if NEXT_PUBLIC_LIFF_ID
 * isn't configured or init itself threw (e.g. strict browser tracking
 * prevention blocking the storage access the SDK needs - see the Edge
 * fallback work this call site exists to support). Never rejects, so
 * every caller can await it directly without its own try/catch.
 */
export function ensureLiffInit(): Promise<boolean> {
  if (!initPromise) {
    initPromise = (async () => {
      const liffId = process.env.NEXT_PUBLIC_LIFF_ID;
      if (!liffId) {
        console.error("[Liff] NEXT_PUBLIC_LIFF_ID is not configured.");
        return false;
      }
      try {
        await liff.init({ liffId });
        return true;
      } catch (error) {
        console.error("[Liff] init failed:", error);
        return false;
      }
    })();
  }
  return initPromise;
}

export { liff };
