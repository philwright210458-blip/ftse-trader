import { handleAPI, canRead, D1Store, json } from "./api.mjs";
// The build embeds the assets here; no asset server or third-party CDN is required.
const assets = __EMBEDDED_ASSETS__;
export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.pathname.startsWith("/api/")) {
        if (!env.DB)
          return json({ error: "Shared storage is not configured." }, 503);
        return await handleAPI(request, env, new D1Store(env.DB));
      }
      if (!canRead(request, env))
        return json({ error: "Private dashboard access denied." }, 403);
      if (!["GET", "HEAD"].includes(request.method))
        return new Response("Use GET.", { status: 405 });
      const asset = assets[url.pathname === "/" ? "/index.html" : url.pathname];
      if (!asset) return new Response("Not found.", { status: 404 });
      return new Response(request.method === "HEAD" ? null : asset.body, {
        headers: {
          "content-type": asset.type,
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          "referrer-policy": "no-referrer",
          "content-security-policy":
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
        },
      });
    } catch {
      return json({ error: "Dashboard storage or service unavailable." }, 503);
    }
  },
};
