/** Static export served by the FastAPI control plane (same origin, no Node server in production).
 *  In `npm run dev`, API calls are proxied to NOESIS_API (default http://127.0.0.1:8000). */
const isDev = process.env.NODE_ENV === "development";
export default {
  output: isDev ? undefined : "export",
  trailingSlash: false,
  images: { unoptimized: true },
  poweredByHeader: false,
  ...(isDev && {
    async rewrites() {
      return [{ source: "/api/:path*", destination: `${process.env.NOESIS_API || "http://127.0.0.1:8000"}/api/:path*` }];
    },
  }),
};
