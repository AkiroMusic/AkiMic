/* AkiMic desktop — dashboard logic (signaling client, audio sink, VU, i18n) */
'use strict';

const $ = (id) => document.getElementById(id);

const S = {
  info: null, settings: {}, platform: null,
  lang: 'en_us', theme: 'dark', i18n: {},
  socket: null, pc: null,
  audioCtx: null, gainNode: null, analyser: null, vuBuf: null, pumpEl: null,
  rafId: 0, statsTimer: 0, statsLevel: 0,
  rttEma: null, lossEma: null, lastLost: null, lastRecv: null,
  streamId: null, pendingIce: [], pairUrl: '', desktopToken: '', desktopJoined: false,
  muted: false, playing: false, phoneHere: false,
  devices: [], selectedDevice: 'default', micLabel: '',
};

const rtcConfig = { iceServers: [] };

/* ---------- i18n ---------- */

function t(key) { return S.i18n[key] || key; }

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (!S.i18n[key]) return;
    const target = el.getAttribute('data-i18n-target');
    if (target === 'title') el.title = S.i18n[key];
    else if (target === 'label') el.setAttribute('label', S.i18n[key]);
    else el.innerHTML = S.i18n[key];
  });
  $('lang-label').textContent = S.lang === 'zh_cn' ? 'EN' : '中';
  renderFooter();
}

async function loadLang(code) {
  try {
    const res = await fetch(`/lang/${code}.json?t=${Date.now()}`);
    if (!res.ok) throw new Error('bad status');
    S.i18n = await res.json();
    S.lang = code;
    applyI18n();
    api('/api/settings', 'POST', { lang: code });
    api('/api/lang', 'POST', { lang: code });       // tray strings
  } catch (e) { console.warn('lang load failed', code, e); }
}

/* ---------- small helpers ---------- */

async function api(url, method = 'GET', body = null) {
  try {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    // per-run CSRF token: management POSTs require it, cross-origin pages
    // cannot set custom headers without a preflight we never grant
    if (S.desktopToken) headers['X-AkiMic-Token'] = S.desktopToken;
    const res = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    return await res.json();
  } catch (e) { return null; }
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(t('d_copied'), 'ok');
  } catch (e) {
    toast(t('d_copy_fail'), 'err');
  }
}

/* ---------- theme ---------- */

const THEMES = ['dark', 'amber', 'mint', 'pearl'];

function applyTheme(name) {
  S.theme = name;
  document.documentElement.dataset.theme = name === 'dark' ? '' : name;
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(S.theme) + 1) % THEMES.length];
  applyTheme(next);
  api('/api/settings', 'POST', { theme: next });
}

/* ---------- UI state ---------- */

function setPhonePill(state) {
  const pill = $('phone-pill');
  pill.className = `pill ${state === 'good' ? 'good' : state === 'bad' ? 'bad' : 'waiting'}`;
}

function setStreamPill(state) {
  const pill = $('stream-pill');
  const text = $('stream-pill-text');
  if (state === 'live') { pill.className = 'pill good'; text.textContent = t('d_stream_live'); }
  else if (state === 'muted') { pill.className = 'pill bad'; text.textContent = t('d_muted_label'); }
  else { pill.className = 'pill accent'; text.textContent = t('d_stream_idle'); }
}

function setMicPill(state) {
  const pill = $('mic-pill');
  const text = $('mic-pill-text');
  if (state === 'live') { pill.className = 'pill good'; text.textContent = t('d_live_label'); }
  else if (state === 'muted') { pill.className = 'pill bad'; text.textContent = t('d_muted_label'); }
  else { pill.className = 'pill waiting'; text.textContent = t('d_mic_idle'); }
}

function showWaitPanel(show) {
  $('panel-wait').hidden = !show;
  $('panel-live').hidden = show;
}

const MIC_ON = '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>';
const MIC_OFF = '<line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/><line x1="12" y1="19" x2="12" y2="23"/>';

function updateMicUI() {
  const btn = $('mic-btn'), icon = $('mic-icon');
  if (!S.playing) {
    btn.className = 'mic-btn';
    icon.innerHTML = MIC_ON;
    setMicPill('idle');
  } else if (S.muted) {
    btn.className = 'mic-btn muted';
    icon.innerHTML = MIC_OFF;
    setMicPill('muted');
  } else {
    btn.className = 'mic-btn live';
    icon.innerHTML = MIC_ON;
    setMicPill('live');
  }
  setStreamPill(S.playing ? (S.muted ? 'muted' : 'live') : 'idle');
}

function renderFooter() {
  if (!S.platform) return;
  const driverOk = S.platform.driver_installed;
  const fwOk = S.platform.firewall_ok;
  $('foot-left').innerHTML =
    `<span class="${driverOk ? 'ok' : 'miss'}">${driverOk ? t('d_driver_ready') : t('d_driver_missing')}</span>` +
    `<span class="sep">·</span><span class="${fwOk ? 'ok' : 'miss'}">${fwOk ? t('d_fw_ok') : t('d_fw_skip')}</span>`;
  $('foot-right').textContent = `AkiMic ${S.info ? S.info.version : ''}`;
}

/* ---------- VU meter ---------- */

const SEG_COUNT = 14;
function buildVuMeter() {
  const box = $('vu-meter');
  box.innerHTML = '';
  for (let i = 0; i < SEG_COUNT; i++) {
    const seg = document.createElement('div');
    seg.className = 'vu-seg';
    box.appendChild(seg);
  }
}

function vuLoop() {
  S.rafId = requestAnimationFrame(vuLoop);
  if (!S.analyser) return;
  S.analyser.getByteTimeDomainData(S.vuBuf);
  let sum = 0;
  for (let i = 0; i < S.vuBuf.length; i++) {
    const v = (S.vuBuf[i] - 128) / 128;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / S.vuBuf.length);
  const analyserLevel = Math.min(1, Math.sqrt(rms) * 2.4);
  const level = S.muted ? 0 : Math.max(analyserLevel, S.statsLevel);
  const segs = $('vu-meter').children;
  const lit = Math.round(level * SEG_COUNT);
  for (let i = 0; i < SEG_COUNT; i++) {
    segs[i].className = 'vu-seg' + (i < lit ? (i >= SEG_COUNT - 3 ? ' lit hot' : ' lit') : '');
  }
}

/* ---------- devices ---------- */

function updateChipName() {
  $('device-chip-text').textContent =
    S.micLabel || (S.platform && S.platform.driver_display_name) || 'CABLE Output';
}

async function loadDevices() {
  let devices = [];
  try {
    // Label visibility requires an accepted permission probe. If it is
    // refused, enumerate anyway — routing by deviceId still works, only
    // the friendly names stay hidden.
    let probe = null;
    try {
      probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      if (e && e.name === 'NotAllowedError') {
        probe = null;  // denied: labels stay generic, never auto-retry
      } else {
        // transient (device busy etc.): a single retry after a pause
        await new Promise((r) => setTimeout(r, 800));
        probe = await navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null);
      }
    }
    if (probe) probe.getTracks().forEach((tr) => tr.stop());

    devices = await navigator.mediaDevices.enumerateDevices();
  } catch (e) {
    console.warn('enumerateDevices failed', e);
  }
  if (!devices.length) return;

  S.devices = devices.filter((d) => d.kind === 'audiooutput');
  const inputs = devices.filter((d) => d.kind === 'audioinput');

  // Show the mic endpoint's live name in the hint chip — self-heals if the
  // device was ever renamed outside this app.
  const cableInput = inputs.find((d) => d.label && /cable/i.test(d.label));
  S.micLabel = cableInput ? cableInput.label : '';
  updateChipName();

  const sel = $('device-select');
  sel.innerHTML = '';

  const optDefault = document.createElement('option');
  optDefault.value = 'default';
  optDefault.textContent = t('d_device_default');
  sel.appendChild(optDefault);

  let autoPick = 'default';
  const keyword = (S.platform && S.platform.driver_match_keyword) || 'cable input';
  S.devices.forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d.deviceId;
    opt.textContent = d.label || `(${d.deviceId.slice(0, 6)}…)`;
    sel.appendChild(opt);
    if (d.label && d.label.toLowerCase().includes(keyword)) autoPick = d.deviceId;
  });

  const saved = S.settings.output_device_id;
  sel.value = saved && S.devices.some((d) => d.deviceId === saved) ? saved : autoPick;
  S.selectedDevice = sel.value;
  applySink();
}

async function applySink() {
  if (!S.audioCtx || !S.selectedDevice || S.selectedDevice === 'default') {
    return true;  // system default routing: nothing explicit to set
  }
  if (typeof S.audioCtx.setSinkId !== 'function') return false;  // unsupported
  try {
    await S.audioCtx.setSinkId(S.selectedDevice);
    return true;
  } catch (e) {
    // one retry — the device list can lag a fresh stream briefly
    await new Promise((r) => setTimeout(r, 500));
    try {
      await S.audioCtx.setSinkId(S.selectedDevice);
      return true;
    } catch (e2) {
      return false;
    }
  }
}

/* ---------- WebRTC ---------- */

/* Dispose the audio graph only — MUST NOT touch the PeerConnection.
   The PC that fired ontrack is mid-negotiation here; closing it would kill
   the connection right as the answer goes out (the old "connected but no
   audio" root cause). PC lifecycle belongs to the offer handler and
   peer_left. */
function disposeAudioGraph() {
  if (S.rafId) { cancelAnimationFrame(S.rafId); S.rafId = 0; }
  clearInterval(S.statsTimer); S.statsTimer = 0;
  if (S.pumpEl) { try { S.pumpEl.srcObject = null; } catch (e) {} S.pumpEl = null; }
  if (S.audioCtx) { try { S.audioCtx.close(); } catch (e) {} S.audioCtx = null; }
  S.gainNode = null; S.analyser = null; S.vuBuf = null;
  S.playing = false;
  S.streamId = null;
  S.statsLevel = 0;
  S.rttEma = null; S.lossEma = null; S.lastLost = null; S.lastRecv = null;
  $('rtt-value').textContent = '—';
  $('loss-value').textContent = '—';
  updateMicUI();
}

function closePeer() {
  S.pendingIce = [];
  if (S.pc) { try { S.pc.close(); } catch (e) {} S.pc = null; }
}

/* Full teardown: the phone is gone or the app is ending. */
function fullTeardown() {
  closePeer();
  disposeAudioGraph();
}

function setupPipeline(stream) {
  if (!stream) return;
  if (S.playing && S.streamId === stream.id) return;  // duplicate ontrack
  disposeAudioGraph();

  // Chromium quirk: a remote WebRTC stream only reliably feeds WebAudio when
  // something "consumes" the track. A muted element keeps the pipeline alive
  // without audible double-playback (the WebAudio graph does the real output).
  const pump = document.createElement('audio');
  pump.muted = true;
  pump.srcObject = stream;
  pump.play().catch(() => {});
  S.pumpEl = pump;

  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const source = ctx.createMediaStreamSource(stream);
  const gain = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(gain);
  gain.connect(analyser);
  analyser.connect(ctx.destination);

  gain.gain.value = S.muted ? 0 : parseFloat($('gain-slider').value);

  S.audioCtx = ctx; S.gainNode = gain; S.analyser = analyser;
  S.vuBuf = new Uint8Array(analyser.fftSize);
  S.streamId = stream.id;
  S.playing = true;

  applySink().then((routed) => {
    if (!routed) toast(t('d_sink_fail'), 'warn');
  });
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});
  updateMicUI();
  vuLoop();
  startStatsLoop(S.pc);
}

function startStatsLoop(pc) {
  clearInterval(S.statsTimer);
  S.statsTimer = setInterval(async () => {
    // stats only from the connection that is still current
    if (!pc || pc !== S.pc || pc.connectionState === 'closed') return;
    try {
      const stats = await pc.getStats();
      let rtt = null;
      let jitterMs = null;
      let audioLevel = 0;
      stats.forEach((r) => {
        if (r.type === 'candidate-pair' && r.state === 'succeeded'
            && r.currentRoundTripTime != null) {
          if (rtt == null || r.currentRoundTripTime < rtt) rtt = r.currentRoundTripTime;
        } else if (r.type === 'inbound-rtp' && r.kind === 'audio') {
          if (r.jitterBufferDelay != null && r.jitterBufferEmittedCount > 0) {
            jitterMs = (r.jitterBufferDelay / r.jitterBufferEmittedCount) * 1000;
          }
          if (r.audioLevel != null) audioLevel = Math.max(audioLevel, r.audioLevel);
          // windowed loss rate from cumulative counters
          if (r.packetsLost != null && r.packetsReceived != null) {
            const lost = Math.max(0, r.packetsLost - (S.lastLost ?? r.packetsLost));
            const recv = Math.max(0, r.packetsReceived - (S.lastRecv ?? r.packetsReceived));
            S.lastLost = r.packetsLost;
            S.lastRecv = r.packetsReceived;
            if (recv + lost > 0) {
              const rate = (lost * 100) / (recv + lost);
              S.lossEma = S.lossEma == null ? rate : S.lossEma * 0.7 + rate * 0.3;
            }
          }
        }
      });
      const shown = rtt != null ? rtt * 1000 : jitterMs;
      if (shown != null) {
        // EMA keeps the readout from twitching per sample
        S.rttEma = S.rttEma == null ? shown : Math.round(S.rttEma * 0.5 + shown * 0.5);
        $('rtt-value').textContent = String(S.rttEma);
      }
      $('loss-value').textContent = S.lossEma == null ? '—' : S.lossEma.toFixed(1) + '%';
      // stats-level fallback drives the VU if the analyser path stays silent
      S.statsLevel = S.playing && !S.muted ? Math.min(1, audioLevel * 2.5) : 0;
    } catch (e) { /* stats unavailable */ }
  }, 1000);
}

async function flushIce(pc) {
  const queued = S.pendingIce.splice(0);
  for (const c of queued) {
    try { await pc.addIceCandidate(new RTCIceCandidate(c)); }
    catch (e) { /* stale or invalid candidate for this session */ }
  }
}

async function handleOffer(offer) {
  closePeer();          // old PC (if any) goes here…
  disposeAudioGraph();  // …and the old audio graph with it
  const pc = new RTCPeerConnection(rtcConfig);
  S.pc = pc;
  S.pendingIce = [];

  pc.onicecandidate = (ev) => { if (ev.candidate) S.socket.emit('ice_candidate', ev.candidate); };
  pc.ontrack = (ev) => {
    const stream = ev.streams && ev.streams[0] ? ev.streams[0] : new MediaStream([ev.track]);
    setupPipeline(stream);
  };
  pc.onconnectionstatechange = () => {
    if (!S.phoneHere) return;
    if (pc.connectionState === 'connected') setStreamPill(S.muted ? 'muted' : 'live');
    else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
      setStreamPill('idle');
    }
  };

  await pc.setRemoteDescription(new RTCSessionDescription(offer));
  // candidates queued while the remote description was pending
  await flushIce(pc);
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  S.socket.emit('answer', answer);
}

function connectSocket() {
  const socket = io(window.location.origin, { reconnection: true, reconnectionDelay: 1000 });
  S.socket = socket;

  socket.on('connect', () => joinDesktop());

  socket.on('peer_joined', (data) => {
    if (data.role !== 'mobile') return;
    S.phoneHere = true;
    setPhonePill('good');
    $('phone-pill-text').textContent = t('d_status_connected');
    showWaitPanel(false);
  });

  socket.on('peer_left', (data) => {
    if (data.role !== 'mobile') return;
    S.phoneHere = false;
    setPhonePill('bad');
    $('phone-pill-text').textContent = t('d_status_reconnect');
    showWaitPanel(true);
    fullTeardown();
  });

  socket.on('offer', (data) => handleOffer(data).catch((e) => console.warn('offer failed', e)));

  socket.on('ice_candidate', (data) => {
    const pc = S.pc;
    if (!pc || pc.signalingState === 'closed') return;
    if (!pc.remoteDescription) { S.pendingIce.push(data); return; }
    pc.addIceCandidate(new RTCIceCandidate(data)).catch(() => {});
  });

  socket.on('room_state', (data) => {
    // authoritative snapshot: true connects, false resets stale state
    // (e.g. after our own socket reconnects while the phone is gone)
    S.desktopJoined = true;
    if (S.drawQr) S.drawQr();  // joined → safe to show the pairing QR now
    if (!data) return;
    if (data.mobile_online && !S.phoneHere) {
      S.phoneHere = true;
      setPhonePill('good');
      $('phone-pill-text').textContent = t('d_status_connected');
      showWaitPanel(false);
    } else if (!data.mobile_online && S.phoneHere) {
      S.phoneHere = false;
      setPhonePill('bad');
      $('phone-pill-text').textContent = t('d_status_reconnect');
      showWaitPanel(true);
      fullTeardown();
    }
  });

  socket.on('disconnect', () => {
    // signaling lost: nothing about the phone state can be trusted anymore
    S.desktopJoined = false;
    if (S.phoneHere) fullTeardown();
    S.phoneHere = false;
    setPhonePill('bad');
    $('phone-pill-text').textContent = t('d_status_reconnect');
    showWaitPanel(true);
  });

  function joinDesktop() {
    if (!S.desktopJoined && S.desktopToken) {
      S.socket.emit('join', { role: 'desktop', token: S.desktopToken });
    }
  }
}

/* ---------- mute / gain ---------- */

async function toggleMute() {
  if (!S.audioCtx) return;
  if (S.audioCtx.state === 'suspended') await S.audioCtx.resume().catch(() => {});
  S.muted = !S.muted;
  S.gainNode.gain.value = S.muted ? 0 : parseFloat($('gain-slider').value);
  updateMicUI();
  api('/api/mute', 'POST', { muted: S.muted });
}

/* ---------- driver flows ---------- */

function setOverlay(state, titleKey, descText) {
  const ov = $('overlay-install');
  ov.hidden = false;
  ov.className = `overlay ${state}`;
  $('install-spinner').style.display = state === 'busy' ? 'block' : 'none';
  $('install-ok').className = state === 'ok' ? 'result-icon ok' : 'result-icon';
  $('install-err').className = state === 'err' ? 'result-icon err' : 'result-icon';
  $('install-title').textContent = t(titleKey);
  if (descText !== undefined) $('install-desc').textContent = descText;
  $('install-retry').hidden = state !== 'err';
}

async function runInstall() {
  setOverlay('busy', 'd_install_title');
  const res = await api('/api/driver/install', 'POST');
  if (res && res.success) {
    S.platform.driver_installed = true;
    renderFooter();
    $('btn-uninstall').hidden = false;
    setOverlay('ok', 'd_install_success');
    loadDevices();
    setTimeout(() => { $('overlay-install').hidden = true; }, 1400);
    const warn = S.platform.post_install_warning;
    if (warn) toast(warn, 'warn');
  } else {
    setOverlay('err', 'd_install_fail', (res && res.msg) || '');
  }
}

async function runUninstall() {
  $('modal-uninstall').hidden = true;
  setOverlay('busy', 'd_uninstall_doing');
  const res = await api('/api/driver/uninstall', 'POST');
  if (res && res.success) {
    setOverlay('ok', 'd_uninstall_ok');
    api('/api/exit', 'POST');   // backend quits the process
  } else {
    setOverlay('err', 'd_uninstall_fail', (res && res.msg) || '');
    toast(t('d_uninstall_fail'), 'err');
  }
}

/* ---------- boot ---------- */

async function init() {
  buildVuMeter();

  const [info, settings, platform] = await Promise.all([
    api('/api/info'), api('/api/settings'), api('/api/platform'),
  ]);
  S.info = info || {};
  S.settings = settings || {};
  S.platform = platform || {};

  $('brand-ver').textContent = S.info.version || '';
  $('about-ver').textContent = S.info.version || '';

  // pairing URL + desktop credential — loopback-only endpoint
  const pair = await api('/api/pair_url');
  if (pair && pair.url) {
    S.pairUrl = pair.url;
    S.desktopToken = pair.desktop_token || '';
  } else {
    S.pairUrl = S.info.url || '';
  }
  $('url-text').textContent = S.pairUrl;

  applyTheme(S.settings.theme || 'dark');

  const lang = !S.settings.lang || S.settings.lang === 'auto'
    ? (navigator.language.toLowerCase().startsWith('zh') ? 'zh_cn' : 'en_us')
    : S.settings.lang;
  await loadLang(lang);

  // QR is drawn only once the socket has joined (authoritative room_state) —
  // a QR shown before that could pair a phone with a half-ready desktop
  // QR (dark modules on white box, fixed for scanning contrast)
  function drawQr() {
    if (!S.pairUrl || $('qrcode').childElementCount) return;
    new QRCode($('qrcode'), {
      text: S.pairUrl, width: 150, height: 150,
      colorDark: '#0C1220', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.H,
    });
  }
  S.drawQr = drawQr;
  if (S.socket && S.socket.connected) drawQr();

  // gain
  const savedGain = parseFloat(S.settings.desktop_gain);
  if (!isNaN(savedGain)) $('gain-slider').value = savedGain;
  $('gain-value').textContent = '×' + parseFloat($('gain-slider').value).toFixed(1);

  // driver
  renderFooter();
  if (S.platform.driver_installed) {
    $('btn-uninstall').hidden = false;
    loadDevices();
  } else if (S.platform.is_admin === false) {
    toast(t('d_need_admin'), 'warn');
  } else {
    runInstall();
  }

  // re-route when devices appear/disappear (driver reinstall, default switch)
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', () => { loadDevices(); });
  }

  connectSocket();
  updateMicUI();

  /* ---- event wiring ---- */
  $('btn-copy-url').addEventListener('click', () => copyText(S.pairUrl || S.info.url || ''));
  $('device-chip').addEventListener('click', () =>
      copyText((S.platform && S.platform.driver_display_name) || 'CABLE Output'));
  $('mic-btn').addEventListener('click', toggleMute);
  $('btn-theme').addEventListener('click', cycleTheme);
  $('btn-lang').addEventListener('click', () => loadLang(S.lang === 'zh_cn' ? 'en_us' : 'zh_cn'));

  $('gain-slider').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('gain-value').textContent = '×' + v.toFixed(1);
    if (S.gainNode && !S.muted) S.gainNode.gain.value = v;
  });
  $('gain-slider').addEventListener('change', (e) =>
      api('/api/settings', 'POST', { desktop_gain: parseFloat(e.target.value) }));

  $('device-select').addEventListener('change', (e) => {
    S.selectedDevice = e.target.value;
    applySink();
    api('/api/settings', 'POST', { output_device_id: S.selectedDevice });
  });

  $('btn-about').addEventListener('click', () => { $('modal-about').hidden = false; });
  $('about-close').addEventListener('click', () => { $('modal-about').hidden = true; });

  $('btn-uninstall').addEventListener('click', () => { $('modal-uninstall').hidden = false; });
  $('uninstall-cancel').addEventListener('click', () => { $('modal-uninstall').hidden = true; });
  $('uninstall-confirm').addEventListener('click', runUninstall);
  $('install-retry').addEventListener('click', runInstall);

  $('win-min').addEventListener('click', () => {
    try { window.pywebview.api.minimize(); } catch (e) {}
  });
  $('win-close').addEventListener('click', () => {
    try { window.pywebview.api.close_app(); } catch (e) {}
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      $('modal-about').hidden = true;
      $('modal-uninstall').hidden = true;
    }
  });
}

document.addEventListener('DOMContentLoaded', init);
