/* AkiMic mobile — mic capture, WebRTC sender, auto-reconnect, wake lock */
'use strict';

const $ = (id) => document.getElementById(id);

const S = {
  i18n: {}, lang: 'en_us', settings: {},
  socket: null, pc: null,
  stream: null, audioCtx: null, source: null, gainNode: null,
  analyser: null, vuBuf: null, dest: null,
  rafId: 0, statsTimer: 0, retryTimer: 0, lastAttempt: 0, rttEma: null,
  pendingIce: [],
  mode: 'idle',            // idle | starting | live | muted
  pcState: 'new',
  joined: false,           // false | true | 'rejected' — accepted as mobile?
  desktopOnline: false,
  wakeLock: null,
};

// per-run pairing secret handed over in the QR URL (?p=...)
const PAIRING_TOKEN = new URLSearchParams(location.search).get('p') || '';

const rtcConfig = { iceServers: [] };

function t(key) { return S.i18n[key] || key; }

/* ---------- i18n ---------- */

async function loadLang(code) {
  try {
    const res = await fetch(`/lang/${code}.json?t=${Date.now()}`);
    if (!res.ok) throw new Error('bad status');
    S.i18n = await res.json();
    S.lang = code;
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.getAttribute('data-i18n');
      if (S.i18n[key]) el.innerHTML = S.i18n[key];
    });
    $('lang-label').textContent = code === 'zh_cn' ? 'EN' : '中';
    updateUI();
    // persist the shared language preference (same key the desktop uses)
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang: code }),
    }).catch(() => {});
  } catch (e) { console.warn('lang load failed', e); }
}

/* ---------- wake lock ---------- */

async function acquireWakeLock() {
  if (!('wakeLock' in navigator) || S.mode !== 'live') return;
  try {
    S.wakeLock = await navigator.wakeLock.request('screen');
  } catch (e) { /* not critical */ }
}

function releaseWakeLock() {
  if (S.wakeLock) {
    S.wakeLock.release().catch(() => {});
    S.wakeLock = null;
  }
}

/* ---------- UI ---------- */

const MIC_ON = '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>';
const MIC_OFF = '<line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/><line x1="12" y1="19" x2="12" y2="23"/>';

function updateUI() {
  const btn = $('mic-btn'), icon = $('mic-icon'), label = $('mic-label');
  if (S.mode === 'idle') {
    btn.className = 'mic-btn';
    icon.innerHTML = MIC_ON;
    label.textContent = t('m_tap_start');
    $('mic-state').textContent = t('m_hint');
  } else if (S.mode === 'starting') {
    btn.className = 'mic-btn';
    icon.innerHTML = MIC_ON;
    label.textContent = t('m_starting');
    $('mic-state').textContent = t('m_wait_conn');
  } else if (S.mode === 'muted') {
    btn.className = 'mic-btn muted';
    icon.innerHTML = MIC_OFF;
    label.textContent = t('m_muted');
    $('mic-state').textContent = t('m_keep_screen');
  } else {
    btn.className = 'mic-btn live';
    icon.innerHTML = MIC_ON;
    label.textContent = t('m_active');
    $('mic-state').textContent = S.pcState === 'connected'
      ? t('m_keep_screen') : t('m_reconnecting');
  }

  // "host online" means the desktop peer is present, not just our socket
  const dot = $('host-dot');
  const text = $('host-text');
  if (S.joined === 'rejected') {
    dot.className = 'dot bad';
    text.textContent = t('m_busy');
  } else if (S.desktopOnline) {
    dot.className = 'dot ok';
    text.textContent = t('m_connected');
  } else {
    dot.className = S.socket && S.socket.connected ? 'dot wait' : 'dot bad';
    text.textContent = t('m_wait_conn');
  }

  const sub = $('link-sub');
  if (S.mode === 'idle') sub.textContent = '';
  else if (S.pcState === 'connected') sub.textContent = 'webrtc: connected';
  else if (S.pcState === 'failed' || S.pcState === 'disconnected') sub.textContent = 'webrtc: retrying…';
  else sub.textContent = 'webrtc: negotiating…';
}

/* ---------- VU ---------- */

const SEG_COUNT = 10;
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
  const level = S.mode === 'live' ? Math.min(1, Math.sqrt(rms) * 2.4) : 0;
  const segs = $('vu-meter').children;
  const lit = Math.round(level * SEG_COUNT);
  for (let i = 0; i < SEG_COUNT; i++) {
    segs[i].className = 'vu-seg' + (i < lit ? (i >= SEG_COUNT - 3 ? ' lit hot' : ' lit') : '');
  }
}

/* ---------- WebRTC ---------- */

function closePc() {
  if (S.pc) { try { S.pc.close(); } catch (e) {} S.pc = null; }
  clearInterval(S.statsTimer); S.statsTimer = 0;
  S.rttEma = null;
  S.pendingIce = [];
}

async function startStreaming() {
  // requires S.stream + audio graph already built
  if (!S.stream || !S.dest || !S.socket || !S.socket.connected) return;
  const now = Date.now();
  if (now - S.lastAttempt < 2000) {
    S.retryTimer = setTimeout(startStreaming, 2100);
    return;
  }
  S.lastAttempt = now;

  closePc();
  const pc = new RTCPeerConnection(rtcConfig);
  S.pc = pc;
  S.pendingIce = [];
  S.pcState = 'new';

  pc.onicecandidate = (ev) => { if (ev.candidate) S.socket.emit('ice_candidate', ev.candidate); };
  pc.onconnectionstatechange = () => {
    S.pcState = pc.connectionState;
    updateUI();
    if (pc.connectionState === 'connected') {
      startStatsLoop();
    } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
      if (S.mode !== 'idle') scheduleRestart();
    }
  };

  S.dest.stream.getTracks().forEach((track) => pc.addTrack(track, S.dest.stream));

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  S.socket.emit('offer', offer);
  updateUI();
}

function scheduleRestart() {
  clearTimeout(S.retryTimer);
  S.retryTimer = setTimeout(() => {
    if (S.mode !== 'idle' && S.socket && S.socket.connected
        && S.pcState !== 'connected') {
      startStreaming().catch(() => scheduleRestart());
    }
  }, 2000);
}

function startStatsLoop() {
  clearInterval(S.statsTimer);
  S.statsTimer = setInterval(async () => {
    if (!S.pc) return;
    try {
      const stats = await S.pc.getStats();
      let rtt = null;
      let jitterMs = null;
      stats.forEach((r) => {
        if (r.type === 'candidate-pair' && r.state === 'succeeded'
            && r.currentRoundTripTime != null) {
          if (rtt == null || r.currentRoundTripTime < rtt) rtt = r.currentRoundTripTime;
        } else if (r.type === 'remote-inbound-rtp' && r.roundTripTime != null) {
          if (rtt == null || r.roundTripTime < rtt) rtt = r.roundTripTime;
        } else if (r.type === 'inbound-rtp' && r.kind === 'audio'
            && r.jitterBufferDelay != null && r.jitterBufferEmittedCount > 0) {
          jitterMs = (r.jitterBufferDelay / r.jitterBufferEmittedCount) * 1000;
        }
      });
      const shown = rtt != null ? rtt * 1000 : jitterMs;
      if (shown != null) {
        S.rttEma = S.rttEma == null ? shown : Math.round(S.rttEma * 0.5 + shown * 0.5);
        $('rtt-value').textContent = `${S.rttEma} ms`;
      }
    } catch (e) { /* ignore */ }
  }, 1000);
}

/* ---------- mic control ---------- */

async function startMic() {
  if (S.mode !== 'idle') return;  // starting guard: no concurrent captures
  S.mode = 'starting';
  updateUI();

  let stream = null;
  let ctx = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
    });

    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const source = ctx.createMediaStreamSource(stream);
    const gain = ctx.createGain();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    const dest = ctx.createMediaStreamDestination();

    // 0 must stay 0 (a saved gain of zero is valid) — no falsy-coalescing here
    const saved = parseFloat($('gain-slider').value);
    gain.gain.value = Number.isFinite(saved) ? saved : 1;

    source.connect(gain);
    gain.connect(analyser);
    gain.connect(dest);

    S.stream = stream; S.audioCtx = ctx; S.source = source; S.gainNode = gain;
    S.analyser = analyser; S.vuBuf = new Uint8Array(analyser.fftSize); S.dest = dest;

    S.mode = 'live';
    updateUI();
    vuLoop();
    acquireWakeLock();
    startStreaming().catch(() => scheduleRestart());
  } catch (e) {
    // any failure: release what we grabbed, back to a clean idle
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (ctx) { try { ctx.close(); } catch (e2) {} }
    S.stream = null; S.audioCtx = null; S.gainNode = null;
    S.analyser = null; S.dest = null;
    $('modal-denied').hidden = false;
    S.mode = 'idle';
    updateUI();
  }
}

function reportMuteState() {
  if (S.socket && S.socket.connected) {
    S.socket.emit('mute_state', { muted: S.mode === 'muted' });
  }
}

function toggleMic() {
  if (S.joined !== true) {
    // rejected (busy/bad pairing) or the desktop has not joined yet
    $('mic-state').textContent = S.joined === 'rejected' ? t('m_busy') : t('m_wait_conn');
    return;
  }
  if (S.mode === 'starting') return;
  if (S.mode === 'idle') {
    startMic();
  } else if (S.mode === 'live') {
    S.mode = 'muted';
    if (S.gainNode) S.gainNode.gain.value = 0;
    releaseWakeLock();
    updateUI();
    reportMuteState();
  } else {
    S.mode = 'live';
    if (S.audioCtx && S.audioCtx.state === 'suspended') S.audioCtx.resume().catch(() => {});
    if (S.gainNode) S.gainNode.gain.value = parseFloat($('gain-slider').value);
    acquireWakeLock();
    updateUI();
    reportMuteState();
    if (S.pcState !== 'connected') scheduleRestart();
  }
}

/* ---------- socket ---------- */

function connectSocket() {
  const socket = io(window.location.origin, { reconnection: true, reconnectionDelay: 1000 });
  S.socket = socket;

  socket.on('connect', () => {
    socket.emit('join', { role: 'mobile', token: PAIRING_TOKEN });
    updateUI();
    reportMuteState();
    if (S.mode !== 'idle' && S.pcState !== 'connected') {
      startStreaming().catch(() => scheduleRestart());
    }
  });

  socket.on('room_state', (data) => {
    // received only after a successful join
    S.joined = true;
    S.desktopOnline = !!(data && data.desktop_online);
    updateUI();
  });

  socket.on('join_rejected', (data) => {
    S.joined = 'rejected';
    updateUI();
  });

  socket.on('peer_joined', (d) => {
    if (d.role === 'desktop') { S.desktopOnline = true; updateUI(); }
  });

  socket.on('peer_left', (d) => {
    if (d.role === 'desktop') { S.desktopOnline = false; updateUI(); }
  });

  socket.on('disconnect', () => { updateUI(); });

  socket.on('answer', async (data) => {
    // ignore stale/duplicate answers (reconnects can race old peers)
    if (!S.pc || S.pc.signalingState !== 'have-local-offer') return;
    try {
      await S.pc.setRemoteDescription(new RTCSessionDescription(data));
      // candidates queued while the remote description was pending
      const queued = S.pendingIce.splice(0);
      for (const c of queued) {
        try { await S.pc.addIceCandidate(new RTCIceCandidate(c)); }
        catch (e) { /* stale or invalid candidate */ }
      }
    } catch (e) { scheduleRestart(); }
  });

  socket.on('ice_candidate', (data) => {
    const pc = S.pc;
    if (!pc || pc.signalingState === 'closed') return;
    if (!pc.remoteDescription) { S.pendingIce.push(data); return; }
    pc.addIceCandidate(new RTCIceCandidate(data)).catch(() => {});
  });

  socket.on('toggle_mute', () => {
    if (S.mode !== 'idle') toggleMic();
  });
}

/* ---------- boot ---------- */

async function init() {
  buildVuMeter();

  try {
    const res = await fetch('/api/settings');
    S.settings = await res.json();
  } catch (e) { S.settings = {}; }

  const lang = !S.settings.lang || S.settings.lang === 'auto'
    ? (navigator.language.toLowerCase().startsWith('zh') ? 'zh_cn' : 'en_us')
    : S.settings.lang;
  await loadLang(lang);

  const saved = parseFloat(S.settings.mobile_gain);
  if (!isNaN(saved)) $('gain-slider').value = saved;
  $('gain-value').textContent = '×' + parseFloat($('gain-slider').value).toFixed(1);

  connectSocket();
  updateUI();

  $('mic-btn').addEventListener('click', toggleMic);
  $('btn-lang').addEventListener('click', () => loadLang(S.lang === 'zh_cn' ? 'en_us' : 'zh_cn'));
  $('denied-ok').addEventListener('click', () => { $('modal-denied').hidden = true; });

  $('gain-slider').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    $('gain-value').textContent = '×' + v.toFixed(1);
    if (S.gainNode && S.mode === 'live') S.gainNode.gain.value = v;
  });
  $('gain-slider').addEventListener('change', (e) => {
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mobile_gain: parseFloat(e.target.value) }),
    }).catch(() => {});
  });

  document.addEventListener('visibilitychange', async () => {
    if (document.visibilityState !== 'visible') return;
    if (S.audioCtx && S.audioCtx.state === 'suspended') {
      await S.audioCtx.resume().catch(() => {});
    }
    if (S.mode === 'live') acquireWakeLock();
    if (S.mode !== 'idle' && S.pcState !== 'connected' && S.socket && S.socket.connected) {
      startStreaming().catch(() => scheduleRestart());
    }
  });
}

document.addEventListener('DOMContentLoaded', init);
