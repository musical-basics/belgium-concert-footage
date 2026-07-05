// Tiny typed-ish client for the Python editor server. Every v2 component talks
// to the backend through this module only — keep fetch() calls out of .svelte
// files so an agent can grep one file to see the whole API surface.

async function j(url, opts) {
  const r = await fetch(url, opts)
  const ct = r.headers.get('content-type') || ''
  if (!ct.includes('application/json')) {
    throw new Error(`non-JSON from ${url} (HTTP ${r.status}) — server stale or down?`)
  }
  const data = await r.json()
  if (data && data.ok === false) throw new Error(data.error || `request failed: ${url}`)
  return data
}

export const api = {
  markers: () => j('/api/markers'),
  meta: () => j('/api/meta'),
  regions: () => j('/api/regions'),
  addRegions: (regions) => j('/api/regions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ regions }),
  }),
  updateRegion: (index, patch) => j('/api/regions/update', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, ...patch }),
  }),
  deleteRegion: (index) => j(`/api/regions?index=${index}`, { method: 'DELETE' }),
  styles: () => j('/api/styles'),
  exportStyle: (style) => j('/api/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ style }),
  }),
  exportPerformance: (index) => j('/api/export', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  }),
  openOutput: (file) => j('/api/open', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file }),
  }),
}
