import type { NextConfig } from "next";

// output:"standalone" makes `next build` emit .next/standalone -- server.js
// plus a pruned node_modules -- which docker/frontend.prod.Dockerfile runs as
// the production image. No effect on `next dev`, which the local `ui` profile
// still uses via docker/frontend.Dockerfile.
const nextConfig: NextConfig = {
  output: "standalone",
};

export default nextConfig;
