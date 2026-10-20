import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Every page shows live data from the API, so pages render per request (no prerendering).
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
