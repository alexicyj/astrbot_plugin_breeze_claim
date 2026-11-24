import { monthGrid } from './calendar.mjs';

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const names = { status: '刷新余额', claim: '领取积分', import: '验证登录态' };
let state, pending = false, timer, dirty = false, seenJob = '', ready = false;
let ttsDirty = false, ttsPending = false, seenTtsJob = '', selectedPreview = '';
let calendarOffset = 0, authStatusSeen = '', voicesCache = [];
let noticeTimer, audition = null;
const voiceLibrary = new Map();
window.lucide?.createIcons();
const fmt = (value) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: state?.config.timezone || 'Asia/Shanghai', hour12: false }) : '--';
const number = (value) => value == null ? '--' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value);
function notice(message, error = false, persistent = false) {
  clearTimeout(noticeTimer);
  $('notice').hidden = false;
  $('notice').textContent = message; $('notice').classList.toggle('error', error);
  if ($('checkin-dialog').open) $('calendar-notice').textContent = message;
  if (!error && !persistent) noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 3000);
}
function canClaim() {
  return ready && !pending && !state?.job?.running && state?.hasSession && state?.summary?.ok && state?.summary?.available && !state?.summary?.claimedToday;
}
function busy() {
  const running = pending || !!state?.job?.running;
  document.querySelectorAll('[data-operation]').forEach((button) => { button.disabled = !ready || running; });
  $('claim').disabled ||= !canClaim();
  renderCalendar();
}
function render() {
  const summary = state.summary;
  const ok = summary?.ok;
  $('balance').textContent = ok ? number(summary.balance) : '--';
  $('amount').textContent = ok ? number(summary.claimedToday ? 0 : summary.amount) : '--';
  $('today').textContent = ok ? (summary.claimedToday ? '今日已签' : summary.available ? '今日可签' : '暂不可签') : '未连接';
  $('reset').textContent = ok ? fmt(summary.nextResetAt) : '--';
  $('updated').textContent = summary?.at ? `更新于 ${fmt(summary.at)}` : '';
  const account = state.account || { state: state.hasSession ? 'bound' : 'unbound' };
  $('session').textContent = { logged_in: '已登录', bound: '已绑定', expired: '登录已失效', unbound: '未绑定' }[account.state] || '未绑定';
  $('session').classList.toggle('expired', account.state === 'expired');
  $('session').classList.toggle('unbound', account.state === 'unbound');
  for (const field of ['name', 'email']) {
    const value = field === 'name' ? account.username : account.email;
    $('account-' + field).textContent = value || '';
    $('account-' + field + '-row').hidden = !value;
  }
  $('verified-at').textContent = fmt(account.verifiedAt);
  if (authStatusSeen !== account.state) {
    authStatusSeen = account.state;
    if (account.state === 'logged_in') $('cookie-entry').open = false;
    if (['unbound', 'expired'].includes(account.state)) $('cookie-entry').open = true;
  }
  $('retries').textContent = `${state.retryCount} / 3`;
  $('server-time').textContent = fmt(state.serverTime);
  if (!dirty) {
    for (const [key, value] of Object.entries(state.config)) {
      if (!$(key)) continue;
      if (typeof value === 'boolean') $(key).checked = value; else $(key).value = value;
    }
  }
  const job = state.job;
  scheduleControls();
  $('task-state').textContent = job?.running ? names[job.action] + '...' : '空闲';
  if (job?.running) notice(names[job.action] + '...', false, true);
  else if (job?.finishedAt && job.finishedAt !== seenJob) {
    seenJob = job.finishedAt;
    notice(job.result.message || (job.result.ok ? '操作完成' : '操作失败'), !job.result.ok);
  }
  $('records').replaceChildren();
  for (const item of state.history) {
    const row = document.createElement('tr');
    for (const value of [fmt(item.at), item.source, item.message || (item.ok ? '完成' : '失败'), item.claimed ? '+' + number(item.credits) : '0', number(item.balance)]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    $('records').append(row);
  }
  $('empty').hidden = state.history.length > 0;
  $('history-count').textContent = state.history.length + ' 笔';
  busy();
  renderTts();
}
async function load() { state = await bridge.apiGet('state'); render(); }
async function run(endpoint, body) {
  pending = true; busy();
  try {
    await bridge.apiPost(endpoint, body);
    if (endpoint === 'session/import') $('cookie').value = '';
    await load();
    if (state?.job?.running) { clearTimeout(timer); timer = setTimeout(poll, 1500); }
    if (endpoint === 'session/clear') notice('登录 Cookie 已删除');
  } catch (error) { notice(error.message, true); }
  finally { pending = false; busy(); }
}
for (const [id, action] of Object.entries({ refresh: 'status', 'refresh-checkin': 'status', claim: 'claim' })) {
  $(id).addEventListener('click', () => run('action', { action }));
}
$('session-form').addEventListener('submit', (event) => { event.preventDefault(); run('session/import', { cookies: $('cookie').value }); });
$('clear').addEventListener('click', () => $('unbind-dialog').showModal());
$('confirm-unbind').addEventListener('click', () => { $('unbind-dialog').close(); run('session/clear', {}); });
$('cookie-website').addEventListener('click', event => {
  if (window.parent === window || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  notice('插件页面限制新窗口，请复制官网地址或右键打开');
});
$('copy-website').addEventListener('click', async () => {
  const input = $('website-url');
  input.focus(); input.select();
  try {
    if (document.execCommand('copy')) { notice('官网地址已复制'); return; }
  } catch {}
  try {
    await navigator.clipboard.writeText(input.value); notice('官网地址已复制');
  } catch { notice('当前浏览器无法自动复制，请选择官网地址', true); }
});
for (const formId of ['config-form']) {
  $(formId).addEventListener('input', () => { dirty = true; });
  $(formId).addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    const keys = $('auto_claim').checked ? ['auto_claim', 'claim_time', 'timezone'] : ['auto_claim'];
    const patch = Object.fromEntries(keys.map((key) => [key, $(key).type === 'checkbox' ? $(key).checked : $(key).value]));
    try { await bridge.apiPost('config', patch); dirty = false; await load(); notice('设置已保存'); }
    catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
}
function scheduleControls() {
  const enabled = $('auto_claim').checked;
  const next = enabled ? state?.nextRunAt : null;
  $('schedule-options').disabled = !enabled;
  $('next-run').textContent = next ? fmt(next) : '-';
  $('next-run').classList.toggle('muted', !next);
}
$('auto_claim').addEventListener('change', scheduleControls);
$('open-history').addEventListener('click', () => $('history-dialog').showModal());
$('open-checkin').addEventListener('click', () => {
  calendarOffset = 0; renderCalendar(); $('checkin-dialog').showModal();
  if (ready && state?.hasSession && !state?.job?.running) run('action', { action: 'status' });
});
document.querySelectorAll('[data-close-dialog]').forEach(button => {
  button.addEventListener('click', () => $(button.dataset.closeDialog).close());
});
function changeMonth(offset) { calendarOffset = Math.max(-120, Math.min(120, offset)); renderCalendar(); }
$('month-prev').addEventListener('click', () => changeMonth(calendarOffset - 1));
$('month-next').addEventListener('click', () => changeMonth(calendarOffset + 1));
$('month-today').addEventListener('click', () => changeMonth(0));
function renderCalendar() {
  if (!state?.calendar?.today) return;
  const month = monthGrid(state.calendar.today, calendarOffset);
  const claimed = new Set(state.calendar.claimedDates || []);
  const grid = $('calendar-grid');
  if (!grid.children.length) {
    for (let index = 0; index < 42; index++) {
      const button = document.createElement('button'); button.type = 'button';
      button.append(document.createElement('span'), document.createElement('small'));
      button.addEventListener('click', () => {
        if (!button.disabled && button.dataset.date === state.calendar.today) run('action', { action: 'claim' });
      });
      grid.append(button);
    }
  }
  month.days.forEach((day, index) => {
    const button = grid.children[index];
    const status = day.isToday ? (state.summary?.claimedToday ? '已签到' : canClaim() ? '可领取' : '暂不可签') : claimed.has(day.key) ? '已签到' : '未记录';
    button.className = ['calendar-day', day.inMonth ? '' : 'outside-month', day.isToday ? 'is-today' : '', claimed.has(day.key) ? 'is-claimed' : ''].filter(Boolean).join(' ');
    button.dataset.date = day.key; button.disabled = !day.inMonth || !day.isToday || !canClaim();
    button.firstChild.textContent = day.day;
    button.lastChild.textContent = day.inMonth && (day.isToday || claimed.has(day.key)) ? status : '';
    button.setAttribute('aria-label', `${day.key} ${day.isToday ? '今天，' : ''}${status}`);
    if (day.isToday) button.setAttribute('aria-current', 'date'); else button.removeAttribute('aria-current');
  });
  $('calendar-month').textContent = month.label;
  $('calendar-reward').textContent = state.summary?.ok && state.summary.amount != null ? '+' + number(state.summary.amount) + ' 积分' : '--';
  $('calendar-count').textContent = '本月签到 ' + [...claimed].filter(key => key.startsWith(month.monthKey)).length + ' 天';
  $('calendar-zone').textContent = state.calendar.timezone;
  $('month-prev').disabled = calendarOffset <= -120;
  $('month-next').disabled = calendarOffset >= 120;
}
function theme(context) { document.documentElement.classList.toggle('dark', context.isDark); }
async function poll() {
  try { await load(); } catch (error) { notice('连接失败：' + error.message, true); }
  timer = setTimeout(poll, state?.job?.running || state?.ttsJob?.running ? 1500 : 30000);
}
busy();
try {
  if (!bridge) throw new Error('请从 AstrBot WebUI 的插件详情打开此页面');
  theme(await bridge.ready()); ready = true;
  const off = bridge.onContext(theme);
  window.addEventListener('beforeunload', () => { clearTimeout(timer); clearTimeout(noticeTimer); off(); $('cookie').value = ''; });
  await load();
  notice(state.hasSession ? '已连接，正在刷新余额' : '尚未保存登录态', false, state.hasSession);
  if (state.hasSession && !state.job?.running) await run('action', { action: 'status' });
  timer = setTimeout(poll, 1500);
  if (state.tts?.hasApiKey) await ttsAction('voices');
} catch (error) { notice(error.message, true); }

function renderTts() {
  const cfg = state?.tts;
  if (!cfg) return;
  $('key-state').textContent = cfg.hasApiKey ? '已保存，留空不修改' : '未设置';
  if (!ttsDirty) {
    $('tts-enabled').checked = cfg.enabled;
    for (const [key, value] of Object.entries(cfg)) {
      if (!$(key) || key === 'enabled' || key === 'api_key') continue;
      if (typeof value === 'boolean') $(key).checked = value; else $(key).value = value;
    }
  }
  ttsRanges();
  renderCurrentVoice();
  $('save-key').disabled = !ready;
  $('open-design').disabled = !ready || !cfg.hasApiKey;
  const job = state.ttsJob;
  document.querySelectorAll('[data-tts-operation]').forEach(b => { b.disabled = !ready || ttsPending || !!job?.running || !cfg.hasApiKey; });
  if (job?.running) {
    notice('语音任务执行中...', false, true);
    $('design-notice').textContent = job.action === 'design' ? '正在生成预览...' : '';
  } else if (job?.result && job.id !== seenTtsJob) {
    seenTtsJob = job.id;
    const result = job.result;
    notice(result.message || (result.ok ? '语音操作完成' : '语音操作失败'), !result.ok);
    $('design-notice').textContent = result.message || '';
    if (!result.ok) return;
    if (job.action === 'balance') notice('API 余额：' + number(result.balance));
    if (job.action === 'voices') renderVoices(result.voices);
    if (job.action === 'preview') {
      voiceLibrary.set(result.voiceId, { ...voiceLibrary.get(result.voiceId), id: result.voiceId, name: result.name, previewUrl: result.previewUrl });
      renderCurrentVoice();
      showAudition(result, job.id);
    }
    if (job.action === 'design') renderDesign(result.previews);
    if (job.action === 'save_design') {
      $('voice_id').value = result.voiceId;
      renderVoices([{ id: result.voiceId, name: $('design-name').value, description: $('design-description').value }, ...voicesCache.filter(voice => voice.id !== result.voiceId)]);
      $('design-dialog').close();
    }
  }
  $('current-play').disabled ||= !cfg.voice_id;
}
function ttsRanges() {
  const enabled = $('tts-enabled').checked;
  const smart = $('smart_voice').checked;
  $('reply-policy').disabled = !enabled;
  $('voice_probability').disabled = !enabled || smart;
  $('probability-setting').classList.toggle('inactive', enabled && smart);
  $('prob-value').textContent = $('voice_probability').value + '%';
  $('speed-value').textContent = Number($('speed').value).toFixed(2) + '×';
}
$('tts-form').addEventListener('input', () => { ttsDirty = true; ttsRanges(); });
$('tts-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  const keys = ['api_key', 'voice_id', 'voice_probability', 'smart_voice', 'style_prompt', 'speed', 'language_code', 'max_chars', 'keep_text', 'group_whitelist', 'base_url'];
  const patch = { enabled: $('tts-enabled').checked };
  for (const key of keys) patch[key] = $(key).type === 'checkbox' ? $(key).checked : ['voice_probability','speed','max_chars'].includes(key) ? Number($(key).value) : $(key).value;
  try { await bridge.apiPost('tts/config', patch); $('api_key').value = ''; ttsDirty = false; await load(); notice('语音配置已保存'); }
  catch(error) { notice(error.message, true); } finally { button.disabled = false; }
});
$('save-key').addEventListener('click', async () => {
  const key = $('api_key').value.trim();
  if (!key && !state?.tts?.hasApiKey) { notice('请填写 API Key', true); return; }
  const voiceId = $('voice_id').value.trim();
  const patch = { voice_id: voiceId };
  if (key) patch.api_key = key;
  $('save-key').disabled = true;
  try {
    await bridge.apiPost('tts/config', patch); $('api_key').value = ''; $('voice_id').value = voiceId;
    await load(); notice('API Key 和音色 ID 已保存');
    if (!state.ttsJob?.running) await ttsAction('voices');
  } catch (error) { notice(error.message, true); }
  finally { $('save-key').disabled = false; }
});
async function ttsAction(action, body = {}, playback = null) {
  ttsPending = true; renderTts();
  try {
    const response = await bridge.apiPost('tts/action', { action, ...body });
    if (action === 'preview') {
      audition = { id: response.job.id, audioId: 'current-audio', autoplay: false, voiceId: body.voice_id || state.tts.voice_id, ...playback };
      $('current-audio').pause();
    }
    await load(); clearTimeout(timer); timer = setTimeout(poll, 1500);
  }
  catch(error) { notice(error.message, true); } finally { ttsPending = false; renderTts(); }
}
$('current-play').addEventListener('click', () => {
  const audio = $('current-audio');
  if (!audio.paused) { audio.pause(); return; }
  if (audio.getAttribute('src') && audio.dataset.voiceId === state.tts.voice_id) { playAudio(audio); return; }
  const voiceId = state.tts.voice_id;
  const url = libraryUrl(voiceLibrary.get(voiceId)?.previewUrl);
  if (url) {
    audio.src = url; audio.dataset.voiceId = voiceId; audio.dataset.source = 'library'; audio.hidden = false;
    playAudio(audio); return;
  }
  ttsAction('preview', { voice_id: voiceId }, { audioId: 'current-audio', autoplay: true });
});
$('voice-search').addEventListener('submit', event => { event.preventDefault(); ttsAction('voices', { search: $('voice-search-text').value }); });
function audioUrl(value) { return /^audio\/[a-z0-9.+-]+$/i.test(value.mime) ? `data:${value.mime};base64,${value.audio}` : ''; }
function libraryUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}
async function playAudio(audio) {
  document.querySelectorAll('audio').forEach(other => { if (other !== audio) other.pause(); });
  try { await audio.play(); }
  catch (error) {
    if (error.name === 'NotAllowedError') notice('浏览器限制自动播放，请点击播放器播放');
    else notice(audio.dataset.source === 'library' ? '语音库试听加载失败，请稍后重试' : '音频播放失败，请重新合成试听', true);
  }
}
function showAudition(result, jobId) {
  const playback = audition?.id === jobId ? audition : { audioId: 'current-audio', voiceId: result.voiceId };
  if (playback.audioId === 'current-audio' && playback.voiceId !== state.tts.voice_id) return;
  const audio = $(playback.audioId);
  audio.src = result.previewUrl ? libraryUrl(result.previewUrl) : audioUrl(result);
  audio.dataset.source = result.previewUrl ? 'library' : 'synthesis';
  audio.dataset.voiceId = playback.voiceId; audio.hidden = false;
  if (playback.audioId === 'current-audio') audio.scrollIntoView({ block: 'nearest' });
  if (playback.autoplay) playAudio(audio);
}
function currentPlayback() {
  const playing = !$('current-audio').paused;
  const button = $('current-play');
  const label = playing ? '暂停当前音色' : '试听当前音色';
  if (button.getAttribute('aria-label') === label) return;
  button.title = label; button.setAttribute('aria-label', label);
  button.innerHTML = `<i data-lucide="${playing ? 'pause' : 'play'}"></i>`;
  window.lucide?.createIcons();
}
for (const event of ['play', 'pause', 'ended', 'emptied']) $('current-audio').addEventListener(event, currentPlayback);
function renderVoices(voices) {
  voicesCache = voices;
  for (const voice of voices) voiceLibrary.set(voice.id, { ...voiceLibrary.get(voice.id), ...voice });
  const list = $('voice-list'); list.replaceChildren();
  renderCurrentVoice();
  if (!voices.length) { list.textContent = '未找到音色'; return; }
  for (const voice of voices) {
    const row = document.createElement('div'); row.className = 'voice-item' + (voice.id === state.tts.voice_id ? ' selected' : '');
    const info = document.createElement('div'); info.className = 'voice-info';
    const name = document.createElement('strong'); name.textContent = voice.name || voice.id;
    const desc = document.createElement('span'); desc.textContent = [voice.id,voice.language,voice.description].filter(Boolean).join(' · '); info.append(name,desc);
    const tools = document.createElement('div'); tools.className = 'voice-tools';
    const audio = document.createElement('audio'); audio.controls = true; audio.preload = 'none'; audio.hidden = true; audio.dataset.source = 'library';
    const url = libraryUrl(voice.previewUrl);
    const play = document.createElement('button'); play.type = 'button'; play.title = '试听音色'; play.setAttribute('aria-label','试听音色'); play.innerHTML = '<i data-lucide="play"></i>';
    play.addEventListener('click', () => {
      if (!url) { notice('该音色暂无语音库试听音频', true); return; }
      if (!audio.paused) { audio.pause(); return; }
      if (!audio.getAttribute('src')) audio.src = url;
      audio.hidden = false; playAudio(audio);
    });
    const use = document.createElement('button'); use.type = 'button'; use.textContent = voice.id === state.tts.voice_id ? '已选' : '使用'; use.disabled = voice.id === state.tts.voice_id;
    use.addEventListener('click', async () => { use.disabled = true; try { await bridge.apiPost('tts/config', { voice_id: voice.id }); $('voice_id').value = voice.id; await load(); renderVoices(voices); notice('音色已切换'); } catch(error) { use.disabled = false; notice(error.message,true); } });
    tools.append(play,use,audio); row.append(info,tools); list.append(row);
  }
  window.lucide?.createIcons();
}
function renderCurrentVoice() {
  const audio = $('current-audio');
  if (audio.dataset.voiceId && audio.dataset.voiceId !== state?.tts?.voice_id) {
    audio.pause(); audio.removeAttribute('src'); delete audio.dataset.voiceId; audio.load(); audio.hidden = true;
  }
  const voice = voiceLibrary.get(state?.tts?.voice_id);
  $('current-voice-name').textContent = voice?.name || (state?.tts?.voice_id ? '当前音色' : '未选择音色');
  $('current-voice-id').textContent = state?.tts?.voice_id || '';
}
$('open-design').addEventListener('click', () => $('design-dialog').showModal());
$('close-design').addEventListener('click', () => $('design-dialog').close());
$('design-dialog').addEventListener('close', () => $('design-dialog').querySelectorAll('audio').forEach(audio => audio.pause()));
$('design-form').addEventListener('submit', event => { event.preventDefault(); ttsAction('design', { description: $('design-description').value, text: $('design-text').value }); });
function renderDesign(previews) {
  $('design-previews').replaceChildren(); selectedPreview = previews[0]?.id || '';
  previews.forEach((preview,index) => {
    const row = document.createElement('label'); row.className = 'preview-item';
    const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'voice-preview'; radio.value = preview.id; radio.checked = index === 0; radio.addEventListener('change', () => { selectedPreview = radio.value; });
    const text = document.createElement('span'); text.textContent = '音色 ' + (index+1);
    const audio = document.createElement('audio'); audio.controls = true; audio.src = audioUrl(preview);
    row.append(radio,text,audio); $('design-previews').append(row);
  });
  $('design-save').hidden = !previews.length;
}
$('design-save').addEventListener('submit', event => { event.preventDefault(); ttsAction('save_design', { id: selectedPreview, name: $('design-name').value, description: $('design-description').value }); });
