import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// The v2 editor UI. Served by the Python editor server at /v2/ (from ui/dist);
// during development `npm run dev` proxies all data/media routes to it, so the
// Python backend is ALWAYS the single source of truth — the UI never grows its
// own server.
export default defineConfig({
  plugins: [svelte()],
  base: '/v2/',
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/api', '/proxies', '/thumbs', '/waveform.u8', '/editor', '/thumb-view']
        .map(p => [p, { target: 'http://localhost:8000', changeOrigin: true }])
    ),
  },
})
