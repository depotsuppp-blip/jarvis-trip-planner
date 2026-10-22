import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // nextjs.Dockerfile sets NEXT_OUTPUT=standalone to get a self-contained
  // server.js. Everything else (Vercel, `npm run build`) builds as before.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
};

export default nextConfig;
