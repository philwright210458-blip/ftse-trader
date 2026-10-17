import { ScanD1Store, scansAPI } from "./scans-api.mjs";
const assets = __SCAN_ASSETS__;
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (
      (path === "/demo" || path === "/demo/") &&
      url.searchParams.get("demo") !== "1"
    )
      return Response.redirect(url.origin + "/demo?demo=1", 302);
    try {
      if (path.startsWith("/api/scans/"))
        return await scansAPI(request, env, new ScanD1Store(env.DB));
      if (path.startsWith("/api/"))
        return new Response("Not available in the collected-scans Site.", {
          status: 404,
        });
      const key =
        path === "/"
          ? "/scans.html"
          : path === "/demo" || path === "/demo/"
            ? "/index.html"
            : path;
      const asset = assets[key];
      if (!asset) return new Response("Not found", { status: 404 });
      if (!["GET", "HEAD"].includes(request.method))
        return new Response("Use GET", { status: 405 });
      return new Response(request.method === "HEAD" ? null : asset.body, {
        headers: {
          "content-type": asset.type,
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
          "content-security-policy":
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://chatgpt.com",
        },
      });
    } catch {
      return new Response(
        JSON.stringify({
          error: "The scan service is unavailable. Please try again shortly.",
        }),
        {
          status: 503,
          headers: {
            "content-type": "application/json",
            "cache-control": "no-store",
          },
        },
      );
    }
  },
};
