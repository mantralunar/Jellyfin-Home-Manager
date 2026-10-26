const $ = selector => document.querySelector(selector);
const state = { master: { libraries: [], settings: {} }, users: [], libraries: [], accessDirty: new Set() };

const settingDefs = [
  ['PlayDefaultAudioTrack', 'Play default audio track', 'Automatically choose Jellyfin’s default audio track when playback begins.', 'bool'],
  ['SubtitleMode', 'Subtitle mode', 'Controls when Jellyfin automatically selects and displays subtitles.', 'select', ['Default', 'Always', 'OnlyForced', 'None', 'Smart']],
  ['AudioLanguagePreference', 'Preferred audio language', 'Prefer audio tracks matching this language. Enter an ISO language code such as eng, fra, deu, or spa.', 'text'],
  ['SubtitleLanguagePreference', 'Preferred subtitle language', 'Prefer subtitle tracks matching this language. Enter an ISO language code such as eng, fra, deu, or spa.', 'text'],
  ['DisplayMissingEpisodes', 'Display missing episodes', 'Show episode entries that are known to exist but are not currently available in the library.', 'bool'],
  ['DisplayCollectionsView', 'Display collections view', 'Show movie collections as a separate entry in the user’s library navigation.', 'bool'],
  ['HidePlayedInLatest', 'Hide played items in Latest', 'Remove items the user has already watched from Latest rows on the home screen.', 'bool'],
  ['RememberAudioSelections', 'Remember audio choices', 'Remember the audio track a user selects and prefer it for later playback.', 'bool'],
  ['RememberSubtitleSelections', 'Remember subtitle choices', 'Remember the subtitle track a user selects and prefer it for later playback.', 'bool'],
  ['EnableNextEpisodeAutoPlay', 'Autoplay next episode', 'Automatically start the next episode after the current episode finishes.', 'bool']
];

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  let body;
  try { body = await response.json(); } catch { throw Error(`HTTP ${response.status}`); }
  if (!response.ok && response.status !== 207) throw Error(body.error || `HTTP ${response.status}`);
  return body;
}

function notice(message, kind = '') {
  const element = $('#notice'); element.textContent = message; element.className = kind;
}

function configuredLibraryIds() {
  return state.libraries.filter(lib => lib.id !== 'special:playlists').map(lib => lib.id);
}

function hasAccess(user, libraryId) {
  return user.accessMode === 'all' || user.libraryIds.includes(libraryId);
}

function changeAccess(user, libraryId, allowed) {
  if (!allowed && user.accessMode === 'all') {
    user.accessMode = 'selected';
    user.libraryIds = configuredLibraryIds().filter(id => id !== libraryId);
  } else if (user.accessMode === 'selected') {
    const selected = new Set(user.libraryIds);
    if (allowed) selected.add(libraryId); else selected.delete(libraryId);
    user.libraryIds = configuredLibraryIds().filter(id => selected.has(id));
  }
  state.accessDirty.add(user.id); updateAccessSummaries();
}

function accessPayload() {
  return Object.fromEntries(state.users
    .filter(user => state.accessDirty.has(user.id) && !user.disabled)
    .map(user => [user.id, { mode: user.accessMode, libraryIds: user.libraryIds }]));
}

function updateAccessSummaries() {
  const active = state.users.filter(user => !user.disabled);
  document.querySelectorAll('.accessPicker').forEach(picker => {
    const count = active.filter(user => hasAccess(user, picker.dataset.libraryId)).length;
    picker.querySelector('summary').textContent = `Access: ${count} of ${active.length} users`;
  });
  document.querySelectorAll('.accessUser').forEach(label => {
    const user = state.users.find(item => item.id === label.dataset.userId);
    if (!user) return;
    const badges = [];
    if (user.accessMode === 'all') badges.push('all libraries'); else badges.push('selected libraries');
    if (user.administrator) badges.push('administrator');
    if (user.disabled) badges.push('disabled');
    label.querySelector('small').textContent = badges.join(' · ');
  });
}

function buildAccessPicker(row, library) {
  const picker = row.querySelector('.accessPicker'); picker.dataset.libraryId = library.id;
  const panel = picker.querySelector('.accessPanel');
  panel.innerHTML = '<strong>Users with library access</strong><small>Changes are staged until Preview and Apply.</small>';
  for (const user of state.users) {
    const label = document.createElement('label'); label.className = 'accessUser';
    label.dataset.userId = user.id;
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = hasAccess(user, library.id); input.disabled = user.disabled;
    input.onchange = () => changeAccess(user, library.id, input.checked);
    const copy = document.createElement('span'); copy.textContent = user.name;
    const badge = document.createElement('small'); copy.append(badge);
    label.append(input, copy); panel.append(label);
  }
  picker.onpointerdown = event => event.stopPropagation();
}

function render() {
  const list = $('#libraries'); list.innerHTML = '';
  for (const library of state.master.libraries) {
    const row = $('#rowTemplate').content.firstElementChild.cloneNode(true); row.dataset.id = library.id;
    const isPlaylists = library.id === 'special:playlists';
    row.querySelector('.libName strong').textContent = library.name;
    row.querySelector('.libName small').textContent = isPlaylists ? 'Automatic Jellyfin view (resolved separately for each user)' : library.id;
    for (const key of ['myMedia', 'latest']) {
      const input = row.querySelector(`[data-key=${key}]`); input.checked = library[key]; input.onchange = () => { library[key] = input.checked; };
    }
    if (isPlaylists) {
      library.latest = false; row.querySelector('.accessPicker').remove();
      const homeLabel = row.querySelector('[data-key=myMedia]').closest('label');
      homeLabel.querySelector('span').textContent = 'Display on Home screen';
      homeLabel.title = 'Matches Jellyfin’s Playlists “Display on home screen” setting';
      row.querySelector('[data-key=latest]').closest('label').remove(); row.classList.add('specialView');
    } else { buildAccessPicker(row, library); }
    row.ondragstart = event => { if (event.target.closest('.accessPicker')) { event.preventDefault(); return; } row.classList.add('dragging'); };
    row.ondragend = () => { row.classList.remove('dragging'); syncOrder(); }; list.append(row);
  }
  updateAccessSummaries();

  const settings = $('#settings'); settings.innerHTML = '';
  for (const [key, label, description, type, values] of settingDefs) {
    const wrap = document.createElement('label'); wrap.className = `setting setting-${type}`;
    const copy = document.createElement('span'); copy.className = 'settingCopy';
    const title = document.createElement('strong'); title.textContent = label;
    const help = document.createElement('small'); help.textContent = description; copy.append(title, help);
    let input;
    if (type === 'bool') {
      input = document.createElement('input'); input.type = 'checkbox'; input.checked = state.master.settings[key] ?? true; state.master.settings[key] = input.checked; wrap.append(input, copy);
    } else if (type === 'select') {
      input = document.createElement('select'); input.setAttribute('aria-label', label); for (const value of values) input.add(new Option(value, value));
      input.value = state.master.settings[key] ?? values[0]; state.master.settings[key] = input.value; wrap.append(copy, input);
    } else {
      input = document.createElement('input'); input.type = 'text'; input.placeholder = 'Leave blank for no preference'; input.setAttribute('aria-label', label);
      input.value = state.master.settings[key] ?? ''; state.master.settings[key] = input.value; wrap.append(copy, input);
    }
    input.onchange = () => { state.master.settings[key] = type === 'bool' ? input.checked : input.value; }; settings.append(wrap);
  }
}

function syncOrder() {
  const map = Object.fromEntries(state.master.libraries.map(item => [item.id, item]));
  state.master.libraries = [...document.querySelectorAll('.lib')].map(item => map[item.dataset.id]);
}

$('#libraries').ondragover = event => {
  event.preventDefault(); const dragging = $('.dragging'); if (!dragging) return;
  const after = [...$('#libraries').querySelectorAll('.lib:not(.dragging)')].find(item => event.clientY <= item.getBoundingClientRect().top + item.offsetHeight / 2);
  $('#libraries').insertBefore(dragging, after || null);
};

function acceptLiveState(data) {
  state.master = data.master; state.users = data.users; state.libraries = data.libraries; state.accessDirty.clear();
}

async function load() {
  try {
    notice('Connecting to Jellyfin…'); const data = await api('/api/state'); acceptLiveState(data);
    $('#discovery').textContent = data.lastDiscovery ? `Last refreshed ${new Date(data.lastDiscovery).toLocaleString()}` : 'Not refreshed yet'; render();
    notice(`${data.libraries.length} libraries and ${data.users.length} users found. Access is live from Jellyfin.`, 'success');
  } catch (error) { notice(error.message, 'error'); }
}

$('#refresh').onclick = async () => {
  if (state.accessDirty.size && !confirm('Discard staged access changes and reload access from Jellyfin?')) return;
  try {
    notice('Refreshing libraries and live user access…'); const data = await api('/api/refresh', { method: 'POST' }); acceptLiveState(data); render();
    const changes = data.changes; notice(`Refresh complete: ${changes.new.length} new, ${changes.renamed.length} renamed, ${changes.removed.length} removed.`, 'success');
    $('#discovery').textContent = `Last refreshed ${new Date(data.lastDiscovery).toLocaleString()}`;
  } catch (error) { notice(error.message, 'error'); }
};

$('#save').onclick = async () => {
  try { syncOrder(); await api('/api/master', { method: 'PUT', body: JSON.stringify({ master: state.master }) }); notice('Master template saved. Staged access changes wait for Preview and Apply.', 'success'); }
  catch (error) { notice(error.message, 'error'); }
};

$('#preview').onclick = async () => {
  try {
    syncOrder(); notice('Building a per-user preview…');
    const data = await api('/api/preview', { method: 'POST', body: JSON.stringify({ master: state.master, access: accessPayload() }) });
    const panel = $('#previewPanel'); panel.innerHTML = '<h3>Per-user access and Home-screen preview</h3>';
    for (const user of data.users) {
      const box = document.createElement('div'); box.className = 'previewUser';
      const myMedia = user.libraries.filter(item => item.myMedia).map(item => item.name);
      const latest = user.libraries.filter(item => item.latest && item.id !== 'special:playlists').map(item => item.name);
      const hidden = user.libraries.filter(item => !item.myMedia && !item.latest).map(item => item.name);
      const access = user.accessChanges.map(change => `${change.name}: ${change.before ? 'allowed' : 'blocked'} → ${change.after ? 'allowed' : 'blocked'}`);
      box.innerHTML = '<strong></strong><dl><div class="accessPreview"><dt>Access changes</dt><dd data-list="access"></dd></div><div><dt>My Media tiles</dt><dd data-list="media"></dd></div><div><dt>Latest rows</dt><dd data-list="latest"></dd></div><div class="hiddenPreview"><dt>Hidden from both</dt><dd data-list="hidden"></dd></div></dl>';
      box.querySelector('strong').textContent = user.name;
      box.querySelector('[data-list=access]').textContent = access.join('; ') || 'None';
      box.querySelector('[data-list=media]').textContent = myMedia.join(' → ') || 'None';
      box.querySelector('[data-list=latest]').textContent = latest.join(' → ') || 'None';
      box.querySelector('[data-list=hidden]').textContent = hidden.join(', ') || 'None'; panel.append(box);
    }
    const bar = document.createElement('div'); bar.className = 'applyBar';
    bar.innerHTML = '<span>A full user and policy backup is created before anything changes.</span><button>Apply to all users</button>';
    bar.querySelector('button').onclick = apply; panel.append(bar); notice(`Preview ready for ${data.users.length} active users.`, 'success');
  } catch (error) { notice(error.message, 'error'); }
};

async function apply() {
  if (!confirm('Apply the staged access changes and Home template to every active user? A backup will be created first.')) return;
  try {
    notice('Applying access first, then Home settings, and reading every user back…');
    const data = await api('/api/apply', { method: 'POST', body: JSON.stringify({ master: state.master, access: accessPayload() }) });
    const failed = data.results.filter(item => !item.ok);
    if (data.ok) { state.accessDirty.clear(); notice(`Applied and verified ${data.results.length} users. Backup: ${data.backup}`, 'success'); }
    else notice(`${failed.length} user(s) failed verification. Backup: ${data.backup}`, 'error');
  } catch (error) { notice(error.message, 'error'); }
}

load();
