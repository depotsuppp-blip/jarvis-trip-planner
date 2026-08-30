"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ensureLiffInit } from "@/lib/liff";

/**
 * Mounted once in the root layout so liff.init() runs no matter which
 * path the browser first lands on - not just inside the poll page - and
 * handles the redirect LINE's own LIFF proxy requires.
 *
 * LINE does NOT append extra LIFF URL path segments directly onto the
 * real endpoint URL. Opening https://liff.line.me/{liffId}/trip/poll/<id>
 * lands here as this app's bare "/" with that path encoded into a
 * `liff.state` query parameter instead - see
 * https://developers.line.biz/en/docs/liff/opening-liff-app/. Renders
 * nothing; it only ever performs the redirect side effect.
 */
export function LiffInitializer() {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;

    async function init() {
      const ok = await ensureLiffInit();
      if (!ok || cancelled) return;

      if (window.location.pathname !== "/") return;

      const state = new URLSearchParams(window.location.search).get("liff.state");
      if (!state) return;

      const target = decodeURIComponent(state);
      router.replace(target.startsWith("/") ? target : `/${target}`);
    }

    init();
    return () => {
      cancelled = true;
    };
  }, [router]);

  return null;
}
