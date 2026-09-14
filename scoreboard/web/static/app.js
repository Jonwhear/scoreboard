/* Scoreboard control panel.
 *
 * Plain ES2020, no build step, no framework.  The page polls /api/status,
 * refreshes the preview image, and pushes every control change straight to
 * /api/config -- the running app picks changes up without a restart.
 */

'use strict';

const STATUS_INTERVAL = 3000;
const PREVIEW_INTERVAL = 1000;

const el = (id) => document.getElementById(id);
const state = {
  config: null,
  statusTimer: null,
  previewTimer: null,
  suppress: false,     // true while we are writing values into the controls
  lastTeamQuery: '',
};

/* -- helpers ----------------------------------------------------------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.error) message = body.error;
    } catch (ignored) { /* not JSON; keep the status line */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = el('toast');
  node.textContent = message;
  node.classList.toggle('error', isError);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), isError ? 5000 : 2200);
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function relativeTime(iso) {
  if (!iso) return 'never';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatUptime(seconds) {
  const total = Math.floor(seconds || 0);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${total % 60}s`;
}

/* -- configuration ----------------------------------------------------- */

let pendingPatch = {};

function mergePatch(target, patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      target[key] = mergePatch(target[key] || {}, value);
    } else {
      target[key] = value;
    }
  }
  return target;
}

const flushPatch = debounce(async () => {
  const patch = pendingPatch;
  pendingPatch = {};
  if (!Object.keys(patch).length) return;
  const saveState = el('save-state');
  saveState.textContent = 'saving…';
  try {
    const result = await api('/api/config', { method: 'POST', body: JSON.stringify(patch) });
    state.config = result.config;
    saveState.textContent = 'saved';
    setTimeout(() => { if (saveState.textContent === 'saved') saveState.textContent = ''; }, 1800);
  } catch (error) {
    saveState.textContent = '';
    toast(`Could not save: ${error.message}`, true);
  }
}, 350);

function updateConfig(patch) {
  if (state.suppress) return;
  mergePatch(pendingPatch, patch);
  if (state.config) mergePatch(state.config, patch);
  flushPatch();
}

/* -- controls ---------------------------------------------------------- */

function bindControls() {
  const bindRange = (id, outputId, apply) => {
    const input = el(id);
    const output = el(outputId);
    input.addEventListener('input', () => {
      output.textContent = input.value;
      apply(Number(input.value));
    });
  };

  bindRange('brightness', 'brightness-out', (value) =>
    updateConfig({ display: { brightness: value } }));
  bindRange('screen-seconds', 'screen-seconds-out', (value) =>
    updateConfig({ rotation: { screen_seconds: value } }));

  el('layout-mode').addEventListener('change', (event) =>
    updateConfig({ rotation: { layout_mode: event.target.value } }));

  const switches = [
    ['show-logos', (checked) => ({ display: { show_logos: checked } })],
    ['favorites-only', (checked) => ({ rotation: { favorites_only: checked } })],
    ['show-nonfav', (checked) => ({ rotation: { show_nonfavorite_live: checked } })],
    ['show-upcoming', (checked) => ({ rotation: { show_upcoming: checked } })],
    ['show-finals', (checked) => ({ rotation: { show_recent_finals: checked } })],
  ];
  for (const [id, build] of switches) {
    el(id).addEventListener('change', (event) => updateConfig(build(event.target.checked)));
  }

  el('sleep-enabled').addEventListener('change', (event) => {
    el('sleep-times').hidden = !event.target.checked;
    updateConfig({ sleep: { enabled: event.target.checked } });
  });
  el('sleep-start').addEventListener('change', (event) =>
    updateConfig({ sleep: { start: event.target.value } }));
  el('sleep-end').addEventListener('change', (event) =>
    updateConfig({ sleep: { end: event.target.value } }));

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      const action = button.dataset.action;
      try {
        await api(`/api/actions/${action}`, { method: 'POST' });
        toast(action === 'next' ? 'Advanced to next screen' : 'Refreshing sports data');
        setTimeout(refreshPreview, 300);
      } catch (error) {
        toast(error.message, true);
      }
    });
  });

  document.querySelectorAll('[data-test]').forEach((button) => {
    button.addEventListener('click', async () => {
      const screen = button.dataset.test;
      try {
        await api('/api/actions/test', {
          method: 'POST',
          body: JSON.stringify({ screen, seconds: 30 }),
        });
        toast(screen === 'clear'
          ? 'Back to the live rotation'
          : `Showing ${button.textContent.toLowerCase()}`);
        setTimeout(refreshPreview, 300);
      } catch (error) {
        toast(error.message, true);
      }
    });
  });

  el('team-league').addEventListener('change', () => searchTeams());
  el('team-search').addEventListener('input', debounce(() => searchTeams(), 220));
}

function applyConfig(config) {
  state.suppress = true;
  try {
    el('brightness').value = config.display.brightness;
    el('brightness-out').textContent = config.display.brightness;
    el('screen-seconds').value = config.rotation.screen_seconds;
    el('screen-seconds-out').textContent = config.rotation.screen_seconds;
    el('layout-mode').value = config.rotation.layout_mode;
    el('show-logos').checked = config.display.show_logos;
    el('favorites-only').checked = config.rotation.favorites_only;
    el('show-nonfav').checked = config.rotation.show_nonfavorite_live;
    el('show-upcoming').checked = config.rotation.show_upcoming;
    el('show-finals').checked = config.rotation.show_recent_finals;
    el('sleep-enabled').checked = config.sleep.enabled;
    el('sleep-times').hidden = !config.sleep.enabled;
    el('sleep-start').value = config.sleep.start;
    el('sleep-end').value = config.sleep.end;
  } finally {
    state.suppress = false;
  }
}

/* -- leagues ----------------------------------------------------------- */

async function loadLeagues() {
  const { leagues } = await api('/api/leagues');
  const container = el('league-toggles');
  const select = el('team-league');
  container.innerHTML = '';
  select.innerHTML = '';

  for (const league of leagues) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = league.enabled;
    input.dataset.league = league.id;
    input.addEventListener('change', onLeagueToggle);
    const text = document.createElement('span');
    text.textContent = league.short_name;
    const name = document.createElement('span');
    name.className = 'league-name';
    name.textContent = league.name.replace(league.short_name, '').trim() || league.sport;
    label.append(input, text, name);
    container.append(label);

    const option = document.createElement('option');
    option.value = league.id;
    option.textContent = `${league.short_name} — ${league.name}`;
    select.append(option);
  }
}

function onLeagueToggle() {
  const enabled = [...document.querySelectorAll('#league-toggles input')]
    .filter((input) => input.checked)
    .map((input) => input.dataset.league);
  if (!enabled.length) {
    toast('At least one league must stay enabled', true);
    loadLeagues();
    return;
  }
  updateConfig({ sports: { enabled_leagues: enabled } });
}

/* -- favourites -------------------------------------------------------- */

async function loadFavorites() {
  const { favorites } = await api('/api/favorites');
  const list = el('favorites-list');
  list.innerHTML = '';
  el('favorites-count').textContent = favorites.length
    ? `${favorites.length} selected` : '';

  for (const favorite of favorites) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    if (favorite.color) chip.style.borderLeftColor = favorite.color;

    const league = document.createElement('span');
    league.className = 'league';
    league.textContent = favorite.league;

    const name = document.createElement('span');
    name.textContent = favorite.display_name || favorite.abbreviation || favorite.team_id;

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = `Remove ${name.textContent}`;
    remove.setAttribute('aria-label', `Remove ${name.textContent}`);
    remove.addEventListener('click', async () => {
      try {
        await api(`/api/favorites/${favorite.league}/${favorite.team_id}`, { method: 'DELETE' });
        toast(`Removed ${name.textContent}`);
        await Promise.all([loadFavorites(), searchTeams()]);
      } catch (error) {
        toast(error.message, true);
      }
    });

    chip.append(league, name, remove);
    list.append(chip);
  }
}

async function searchTeams() {
  const league = el('team-league').value;
  const query = el('team-search').value.trim();
  if (!league) return;
  const results = el('team-results');
  state.lastTeamQuery = `${league}:${query}`;
  let payload;
  try {
    payload = await api(`/api/teams?league=${encodeURIComponent(league)}`
      + `&q=${encodeURIComponent(query)}&limit=40`);
  } catch (error) {
    results.innerHTML = '';
    results.append(emptyRow(`Could not load teams: ${error.message}`));
    return;
  }
  if (state.lastTeamQuery !== `${league}:${query}`) return;  // a newer search won

  results.innerHTML = '';
  if (!payload.teams.length) {
    results.append(emptyRow(payload.loading
      ? 'Downloading the team list for this league…'
      : 'No teams matched.'));
    if (payload.loading) setTimeout(searchTeams, 1500);
    return;
  }

  for (const team of payload.teams) {
    const row = document.createElement('li');

    if (team.logo_url) {
      const logo = document.createElement('img');
      logo.className = 'logo';
      logo.loading = 'lazy';
      logo.alt = '';
      logo.src = team.logo_url;
      logo.addEventListener('error', () => logo.remove());
      row.append(logo);
    }

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = team.display_name;

    const abbr = document.createElement('span');
    abbr.className = 'abbr';
    abbr.textContent = team.abbreviation;

    const add = document.createElement('button');
    add.className = 'add';
    add.type = 'button';
    add.textContent = team.favorite ? 'Added' : 'Add';
    add.disabled = team.favorite;
    add.addEventListener('click', async () => {
      add.disabled = true;
      try {
        await api('/api/favorites', {
          method: 'POST',
          body: JSON.stringify({
            league: team.league,
            team_id: team.team_id,
            abbreviation: team.abbreviation,
            display_name: team.display_name,
          }),
        });
        add.textContent = 'Added';
        toast(`Added ${team.display_name}`);
        await Promise.all([loadFavorites(), loadLeagues(), refreshConfig()]);
      } catch (error) {
        add.disabled = false;
        toast(error.message, true);
      }
    });

    row.append(name, abbr, add);
    results.append(row);
  }
}

function emptyRow(text) {
  const row = document.createElement('li');
  row.className = 'empty';
  row.textContent = text;
  return row;
}

/* -- status ------------------------------------------------------------ */

function statusRow(grid, term, value, className) {
  const dt = document.createElement('dt');
  dt.textContent = term;
  const dd = document.createElement('dd');
  if (className) dd.className = className;
  if (value instanceof Node) dd.append(value);
  else dd.textContent = value;
  grid.append(dt, dd);
}

function coloured(text, tone) {
  const span = document.createElement('span');
  span.className = tone;
  span.textContent = text;
  return span;
}

async function refreshStatus() {
  let status;
  try {
    status = await api('/api/status');
  } catch (error) {
    el('health-dot').className = 'brand-dot bad';
    el('header-status').textContent = 'scoreboard unreachable';
    return;
  }

  const dot = el('health-dot');
  const header = el('header-status');
  const screen = status.screen || {};

  if (!status.online) {
    dot.className = 'brand-dot bad';
  } else if (status.stale || status.display_error) {
    dot.className = 'brand-dot warn';
  } else {
    dot.className = 'brand-dot ok';
  }
  header.textContent = status.sleeping
    ? 'asleep'
    : `${screen.title || 'idle'} · ${screen.layout || '-'}`;

  const grid = el('status-grid');
  grid.innerHTML = '';
  statusRow(grid, 'Display', status.display_error
    ? coloured(`${status.display_backend} — ${status.display_error}`, 'bad')
    : coloured(`${status.display_backend} running`, 'good'));
  statusRow(grid, 'Sports data', status.online
    ? (status.stale ? coloured('stale — using cached data', 'warn') : coloured('ok', 'good'))
    : coloured('offline — using cached data', 'bad'));
  statusRow(grid, 'Last update', relativeTime(status.last_success));
  statusRow(grid, 'Showing', screen.total
    ? `${screen.title} (${screen.index}/${screen.total}, ${screen.layout})`
    : (screen.title || '—'));
  if (status.sleeping) statusRow(grid, 'Sleep', coloured('asleep', 'warn'));
  statusRow(grid, 'Uptime', formatUptime(status.uptime_seconds));
  statusRow(grid, 'Config', `${status.config_source} · ${status.config_path}`, 'mono');

  const leagueLines = (status.leagues || []).map((league) => {
    const parts = [`${league.league}: ${league.game_count} game(s)`];
    // error_short keeps a transport failure to a few words; the full text is
    // in the journal.
    if (league.error_short) parts.push(league.error_short);
    return parts.join(' — ');
  });
  statusRow(grid, 'Leagues', leagueLines.length ? leagueLines.join('\n') : 'none polled yet');

  if (status.logos) {
    statusRow(grid, 'Logos',
      `${status.logos.cached_raw} cached, ${status.logos.failed} unavailable`);
  }

  const matrix = el('matrix-grid');
  matrix.innerHTML = '';
  const m = status.matrix || {};
  statusRow(matrix, 'Panels', `${m.chain_length} × ${m.cols}×${m.rows} (parallel ${m.parallel})`);
  statusRow(matrix, 'Canvas', m.canvas, 'mono');
  statusRow(matrix, 'GPIO mapping', m.gpio_mapping, 'mono');
  statusRow(matrix, 'GPIO slowdown', String(m.slowdown_gpio), 'mono');
  if (status.fonts) {
    statusRow(matrix, 'Fonts', Object.entries(status.fonts)
      .map(([role, source]) => `${role}: ${source}`).join('\n'), 'mono');
  }
}

/* -- preview ----------------------------------------------------------- */

function refreshPreview() {
  const image = el('preview');
  const next = new Image();
  next.onload = () => {
    image.src = next.src;
    image.hidden = false;
    el('preview-empty').hidden = true;
    el('preview-meta').textContent = `${next.naturalWidth} × ${next.naturalHeight}`;
  };
  next.onerror = () => {
    image.hidden = true;
    el('preview-empty').hidden = false;
  };
  next.src = `/api/preview.png?t=${Date.now()}`;
}

/* -- boot -------------------------------------------------------------- */

async function refreshConfig() {
  state.config = await api('/api/config');
  applyConfig(state.config);
}

function startPolling() {
  const visible = () => document.visibilityState === 'visible';
  state.statusTimer = setInterval(() => { if (visible()) refreshStatus(); }, STATUS_INTERVAL);
  state.previewTimer = setInterval(() => { if (visible()) refreshPreview(); }, PREVIEW_INTERVAL);
  document.addEventListener('visibilitychange', () => {
    if (visible()) { refreshStatus(); refreshPreview(); }
  });
}

async function init() {
  bindControls();
  try {
    await refreshConfig();
    await loadLeagues();
    await loadFavorites();
    await searchTeams();
  } catch (error) {
    toast(`Could not load configuration: ${error.message}`, true);
  }
  refreshStatus();
  refreshPreview();
  startPolling();
}

document.addEventListener('DOMContentLoaded', init);
