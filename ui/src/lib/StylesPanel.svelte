<script>
  // Styles dashboard: every render style, its readiness (which performances
  // have the regions it needs), live export progress, and the finished output.
  // First panel of the v2 migration — talks to the Python server via lib/api.js.
  import { onMount, onDestroy } from 'svelte'
  import { api } from './api.js'

  let styles = $state([])
  let perfs = $state([])
  let regions = $state([])
  let error = $state('')
  let pollTimer = null

  const fmtTC = (t) => {
    const s = Math.max(0, Math.round(t)), m = Math.floor(s / 60)
    return `${m}:${String(s % 60).padStart(2, '0')}`
  }

  async function refresh() {
    try {
      const [st, mk] = await Promise.all([api.styles(), api.markers()])
      styles = st.styles || []
      perfs = mk.performances || []
      regions = mk.regions || []
      error = ''
    } catch (e) {
      error = String(e)
    }
    schedule()
  }

  function schedule() {
    clearTimeout(pollTimer)
    const busy = styles.some(s => s.job && ['queued', 'running'].includes(s.job.status))
    pollTimer = setTimeout(refresh, busy ? 900 : 5000)
  }

  // Per-performance readiness for the applause ranker.
  const rows = $derived(perfs.map((p, i) => {
    const ap = regions.find(r => r.kind === 'applause' && r.perf === i)
    const hl = regions.find(r => r.kind === 'highlight' && r.perf === i)
    return { i, title: p.title, applause: ap, highlight: hl }
  }))
  const readyCount = $derived(rows.filter(r => r.applause && r.applause.rank != null).length)

  async function setRank(row, value) {
    const idx = regions.indexOf(row.applause)
    if (idx < 0) return
    const rank = value === '' ? null : +value
    try {
      const res = await api.updateRegion(idx, { rank })
      regions = res.regions
    } catch (e) { error = String(e) }
  }

  async function runStyle(id) {
    try {
      await api.exportStyle(id)
      await refresh()
    } catch (e) { error = String(e) }
  }

  onMount(refresh)
  onDestroy(() => clearTimeout(pollTimer))
</script>

{#if error}<div class="card error">⚠ {error}</div>{/if}

{#each styles.filter(s => !s.per_performance) as s (s.id)}
  <div class="card">
    <div class="head">
      <h2>{s.label}</h2>
      {#if s.job && ['queued', 'running'].includes(s.job.status)}
        <span class="muted">{s.job.phase} · {s.job.progress}%</span>
        <progress max="100" value={s.job.progress}></progress>
      {:else}
        <button class="primary" onclick={() => runStyle(s.id)}
                disabled={s.id === 'applause_ranker' && readyCount === 0}>
          🎬 Export
        </button>
      {/if}
      {#if s.output}
        <button onclick={() => api.openOutput(s.output)}>▶ Open {s.output}</button>
      {/if}
    </div>
    {#if s.needs}<p class="muted">Needs: {s.needs}</p>{/if}
    {#if s.job && s.job.status === 'error'}<p class="error">⚠ {s.job.error}</p>{/if}

    {#if s.id === 'applause_ranker'}
      <p class="muted">{readyCount}/{perfs.length} performances ready
        (applause marked + ranked). Mark regions in the <a href="/">classic editor</a>
        with the 👏/✦ buttons — or let an agent add them via <code>/api/regions</code>.</p>
      <table>
        <thead><tr><th>#</th><th>Piece</th><th>👏 Applause</th><th>Rank</th><th>✦ Highlight</th></tr></thead>
        <tbody>
          {#each rows as r (r.i)}
            <tr class:ready={r.applause && r.applause.rank != null}>
              <td>{r.i + 1}</td>
              <td>{r.title}</td>
              <td>{r.applause ? `${fmtTC(r.applause.in)} → ${fmtTC(r.applause.out)}` : '—'}</td>
              <td>
                {#if r.applause}
                  <input type="number" min="1" max="10" step="0.5"
                         value={r.applause.rank ?? ''}
                         onchange={(e) => setRank(r, e.currentTarget.value)} />
                {:else}—{/if}
              </td>
              <td>{r.highlight ? `${fmtTC(r.highlight.in)} → ${fmtTC(r.highlight.out)}` : 'auto (pre-applause)'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
{/each}

<div class="card">
  <h2>Classic per-performance exports</h2>
  <p class="muted">The landscape highlights render stays in the
    <a href="/">classic editor</a> (Export button on each performance row).
    This page will grow panels as they migrate — see ARCHITECTURE.md.</p>
</div>

<style>
  .head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h2 { font-size: 14px; margin: 0; flex: 1; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td { text-align: left; padding: 5px 8px; border-top: 1px solid var(--line); font-size: 12px; }
  th { color: var(--muted); font-weight: 600; border-top: none; }
  tr.ready td { color: var(--accent2); }
  tr.ready td input { color: var(--accent2); }
  input[type='number'] { width: 60px; }
  progress { accent-color: var(--accent); width: 160px; }
  .error { color: var(--danger); }
  a { color: var(--accent); }
  code { background: var(--panel2); padding: 1px 5px; border-radius: 4px; }
</style>
