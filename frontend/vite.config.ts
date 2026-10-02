import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// base: "/admin/" so built asset paths match where FastAPI mounts the SPA in
// production (docs/archive/UI_REWRITE_PLAN.md §4), and so the dev server mirrors that
// same URL shape - requests the app makes to "/admin/api/*" work identically
// in both dev (proxied below) and prod (same-origin, no proxy involved).
export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/admin/api": {
        target: "http://127.0.0.1:8765",
        // changeOrigin rewrites the outgoing Host header to match the
        // target, but NOT the Origin header - verified empirically
        // (docs/archive/UI_REWRITE_PLAN.md §4/§9 Phase 0.1): with changeOrigin
        // alone, the backend received Origin: http://localhost:5173 but
        // Host: 127.0.0.1:8765, a genuine mismatch, and
        // _origin_is_trusted() correctly 403'd it - the check working
        // exactly as designed. The fix is not to weaken that check; it's
        // to rewrite Origin the same way changeOrigin already rewrites
        // Host, so the backend sees a request that is *actually*
        // same-origin (both headers agree), not a bypass of the check.
        changeOrigin: true,
        configure(proxy) {
          proxy.on("proxyReq", (proxyReq, req) => {
            const origin = req.headers.origin
            if (origin) {
              proxyReq.setHeader("origin", "http://127.0.0.1:8765")
            }
          })
        },
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, "../backend/src/agent_control/static/admin"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const moduleId = id.replaceAll("\\", "/")
          if (!moduleId.includes("/node_modules/")) return undefined
          if (moduleId.includes("/@xyflow/") || moduleId.includes("/d3-")) return "graph"
          if (
            moduleId.includes("/react-markdown/")
            || moduleId.includes("/remark-")
            || moduleId.includes("/rehype-")
            || moduleId.includes("/unified/")
          ) return "markdown"
          if (moduleId.includes("/@base-ui/")) return "vendor"
          if (moduleId.includes("/lucide-react/")) return "icons"
          if (moduleId.includes("/@tanstack/")) return "tanstack"
          if (
            moduleId.includes("/react/")
            || moduleId.includes("/react-dom/")
            || moduleId.includes("/react-router")
          ) return "vendor"
          return undefined
        },
      },
    },
  },
})
