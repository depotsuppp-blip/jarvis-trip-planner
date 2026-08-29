"use client";

import { use, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { StickyActionButton } from "@/components/StickyActionButton";

export default function TripDraftPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Client Component page: params is a Promise even here, read with React's
  // use() rather than await since this component cannot be async.
  const { id } = use(params);

  const [text, setText] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function loadDraft() {
      try {
        const response = await fetch(`/api/draft/${id}`, { cache: "no-store" });
        const data = await response.json();
        if (!cancelled) {
          setText(data.draft?.text ?? "");
          setSavedAt(data.draft?.updatedAt ?? null);
        }
      } catch {
        // Leave the board blank - still usable from scratch.
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    loadDraft();
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleSave() {
    setError("");
    setIsSaving(true);
    try {
      const response = await fetch(`/api/draft/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });

      if (!response.ok) {
        throw new Error("Save failed.");
      }

      const data = await response.json();
      setSavedAt(data.draft?.updatedAt ?? new Date().toISOString());
    } catch {
      setError("Could not save the draft. Please try again.");
    } finally {
      setIsSaving(false);
    }
  }

  function handleGenerateItinerary() {
    alert("Will trigger Jarvis AI in the next phase");
  }

  return (
    <div className="min-h-screen pb-32">
      <PageHeader title="Solo Trip Draft Board" tripId={id} />

      <main className="mx-auto max-w-md space-y-5 px-4 py-6">
        <section className="rounded-3xl border border-white/10 bg-[#1A1A1A] p-5 shadow-2xl shadow-black/40">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-white">Your Wishlist</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Paste links, ideas, restaurant names - anything goes.
              </p>
            </div>
            {savedAt && !isLoading && (
              <span className="shrink-0 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-medium text-emerald-400/90">
                Saved {new Date(savedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </span>
            )}
          </div>

          {isLoading ? (
            <p className="mt-6 text-sm text-zinc-500">Loading your draft...</p>
          ) : (
            <>
              <textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="Paste links, restaurant names, or ideas here..."
                rows={12}
                className="mt-4 w-full resize-none rounded-2xl border border-white/10 bg-[#222222] px-4 py-4 text-base leading-relaxed text-white placeholder:text-zinc-500 shadow-inner shadow-black/30 outline-none transition focus:border-white/25 focus:bg-[#262626] focus:shadow-[0_0_0_4px_rgba(255,255,255,0.05)]"
              />

              <button
                type="button"
                onClick={handleSave}
                disabled={isSaving}
                className="mt-4 w-full rounded-full bg-zinc-200 px-5 py-4 text-base font-semibold text-zinc-900 shadow-md shadow-black/30 transition active:scale-[0.98] disabled:opacity-50"
              >
                {isSaving ? "Saving..." : "Save Draft"}
              </button>

              {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
            </>
          )}
        </section>
      </main>

      <StickyActionButton label="Generate Itinerary" onClick={handleGenerateItinerary} />
    </div>
  );
}
