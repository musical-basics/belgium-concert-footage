<script>
  import StylesPanel from './lib/StylesPanel.svelte'
  document.title = 'Video Editor — Styles'
</script>

<header>
  <h1>🎹 Video Editor — Styles</h1>
  <span class="muted">v2 · the classic marker editor lives at <a href="/">/</a></span>
</header>

<StylesPanel />

<style>
  header { display: flex; align-items: baseline; gap: 12px; margin: 4px 0 14px; }
  h1 { font-size: 17px; margin: 0; }
  a { color: var(--accent); }
</style>
