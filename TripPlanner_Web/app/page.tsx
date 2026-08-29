/**
 * There is no real "home" screen yet: every real entry point is a link
 * Jarvis generates and pushes over LINE (/trip/poll/[id] or
 * /trip/draft/[id]) - nobody is expected to land on "/" directly. This
 * replaces create-next-app's default template splash (Vercel/Next.js
 * logos and marketing links) with something that at least matches the
 * rest of the app's look, rather than looking like an unfinished demo.
 */
export default function Home() {
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-3xl border border-white/10 bg-[#1A1A1A] p-6 text-center shadow-2xl shadow-black/50">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl border border-white/10 bg-[#222222] text-xl shadow-md shadow-black/40">
          ✈️
        </div>
        <h1 className="mt-4 text-lg font-semibold text-white">Trip Planner</h1>
        <p className="mt-2 text-sm text-zinc-400">
          Open the link Jarvis sent you on LINE to view your trip&apos;s poll
          or draft board.
        </p>
      </div>
    </div>
  );
}
