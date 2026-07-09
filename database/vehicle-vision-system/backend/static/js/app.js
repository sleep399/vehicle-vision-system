const App = {
  token: localStorage.getItem('token') || '',
  streamModule: null,
  streamInterval: null,
  wsAlerts: null,
  wsStream: null,
  streamBusy: false,
  streamTimeout: null,
  uploadedRecognitionVideo: null,
  uploadedRecognitionResults: [],
  uploadedLabelInterval: null,

  init() {
    this.bindTabs();
    this.bindNav();
    this.bindFileInputs();
    if (this.token) this.showMain();
    else document.getElementById('login-page').classList.add('active');
  },

  headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  },

  async api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { ...this.headers(), ...opts.headers } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || '请求失败');
    }
    return res.json();
  },

  bindTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.onclick = () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      };
    });
  },

  bindNav() {
    document.querySelectorAll('.nav-item[data-view]').forEach(item => {
      item.onclick = (e) => {
        e.preventDefault();
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        item.classList.add('active');
        document.getElementById('view-' + item.dataset.view).classList.add('active');
        this.onViewChange(item.dataset.view);
      };
    });
  },

  bindFileInputs() {
    document.getElementById('lpr-file').onchange = (e) => this.uploadFile('lpr', e.target.files[0]);
    document.getElementById('police-file').onchange = (e) => this.uploadFile('police', e.target.files[0]);
    document.getElementById('owner-file').onchange = (e) => this.uploadFile('owner', e.target.files[0]);
  },

  onViewChange(view) {
    if (view === 'dashboard') this.loadDashboard();
    if (view === 'lpr') { this.loadLprHistory(); }
    if (view === 'police') { this.ensurePolicePoseBackendControl(); this.loadPolicePoseBackend(); this.loadPoliceGestures(); this.loadPoliceHistory(); }
    if (view === 'owner') { this.loadOwnerGestures(); this.loadVehicleState(); }
    if (view === 'alerts') { this.loadAlerts(); this.connectAlertWs(); }
    if (view === 'logs') this.loadLogs();
  },

  async login() {
    const username = document.getElementById('login-user').value;
    const password = document.getElementById('login-pass').value;
    try {
      const data = await this.api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      this.token = data.access_token;
      localStorage.setItem('token', this.token);
      this.showMain();
    } catch (e) { alert(e.message); }
  },

  async sendCode() {
    const target = document.getElementById('code-target').value;
    try {
      const data = await this.api('/api/auth/send-code', {
        method: 'POST',
        body: JSON.stringify({ target, target_type: target.includes('@') ? 'email' : 'phone' }),
      });
      alert('验证码: ' + data.code + ' (演示模式直接显示)');
    } catch (e) { alert(e.message); }
  },

  async loginCode() {
    const target = document.getElementById('code-target').value;
    const code = document.getElementById('code-input').value;
    try {
      const data = await this.api('/api/auth/login-code', {
        method: 'POST',
        body: JSON.stringify({ target, code, target_type: target.includes('@') ? 'email' : 'phone' }),
      });
      this.token = data.access_token;
      localStorage.setItem('token', this.token);
      this.showMain();
    } catch (e) { alert(e.message); }
  },

  async wechatLogin() {
    try {
      const session = await this.api('/api/auth/wechat/qrcode', { method: 'POST' });
      const qrBox = document.getElementById('qr-box');
      qrBox.innerHTML = `微信扫码登录<br><small>${session.session_id.slice(0, 8)}</small><div class="qr-placeholder">二维码已生成，当前为演示模式</div>`;
      const poll = setInterval(async () => {
        const res = await fetch(session.poll_url);
        const data = await res.json();
        if (data.status === 'confirmed') {
          clearInterval(poll);
          this.token = data.access_token;
          localStorage.setItem('token', this.token);
          this.showMain();
        }
      }, 1500);
    } catch (e) { alert(e.message); }
  },

  skipLogin() { this.showMain(); },

  showMain() {
    document.getElementById('login-page').classList.remove('active');
    document.getElementById('main-page').classList.add('active');
    this.loadDashboard();
    this.connectAlertWs();
    if (this.token) {
      this.api('/api/auth/me').then(u => {
        document.getElementById('user-info').textContent = u.username;
      }).catch(() => {});
    }
  },

  logout() {
    this.token = '';
    localStorage.removeItem('token');
    this.stopStream();
    location.reload();
  },

  connectAlertWs() {
    if (this.wsAlerts) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.wsAlerts = new WebSocket(`${proto}://${location.host}/ws/alerts`);
    this.wsAlerts.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'alert') this.showToast(data);
      this.prependAlert(data);
    };
    this.wsAlerts.onclose = () => { this.wsAlerts = null; setTimeout(() => this.connectAlertWs(), 3000); };
  },

  showToast(alert) {
    const el = document.createElement('div');
    el.className = 'toast ' + (alert.level || '');
    el.innerHTML = `<strong>${alert.title}</strong><br><small>${alert.summary}</small>`;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => el.remove(), 5000);
  },

  prependAlert(alert) {
    const container = document.getElementById('live-alerts');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'alert-item ' + (alert.level || '');
    div.innerHTML = `<div class="alert-title">${alert.title}</div><div>${alert.summary}</div><div class="alert-meta">${alert.created_at || new Date().toISOString()}</div>`;
    container.prepend(div);
  },

  async loadDashboard() {
    try {
      const [lpr, police, owner, stats] = await Promise.all([
        this.api('/api/lpr/history?limit=100'),
        this.api('/api/police-gesture/history?limit=100'),
        this.api('/api/owner-gesture/history?limit=100'),
        this.api('/api/monitor/alerts/stats'),
      ]);
      document.getElementById('stat-lpr').textContent = lpr.length;
      document.getElementById('stat-police').textContent = police.length;
      document.getElementById('stat-owner').textContent = owner.length;
      document.getElementById('stat-alerts').textContent = stats.total;
      const el = document.getElementById('dashboard-alerts');
      el.innerHTML = stats.recent.slice(0, 5).map(a =>
        `<div class="alert-item ${a.level}"><div class="alert-title">${a.title}</div><div>${a.summary}</div></div>`
      ).join('') || '<p style="color:var(--text-muted)">暂无告警</p>';
    } catch (e) { console.error(e); }
  },

  async uploadFile(module, file) {
    if (!file) return;
    const isVideo = file.type.startsWith('video/') || /\.(mp4|avi|mov|mkv|webm|flv)$/i.test(file.name || '');
    const imageEndpoints = { lpr: '/api/lpr/recognize', police: '/api/police-gesture/recognize', owner: '/api/owner-gesture/recognize' };
    const videoEndpoints = { lpr: '/api/lpr/recognize-video', police: '/api/police-gesture/recognize-video', owner: '/api/owner-gesture/recognize-video' };
    const endpoints = isVideo ? videoEndpoints : imageEndpoints;
    const form = new FormData();
    form.append('file', file);
    const headers = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    const previewMap = { lpr: 'lpr-preview', police: 'police-preview', owner: 'owner-preview' };
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const resultBox = document.getElementById(resultMap[module]);
    this.showUploadPreview(module, file, isVideo);
    if (module === 'police' && isVideo) {
      if (resultBox) resultBox.innerHTML = '正在随视频播放实时识别...';
      this.startUploadedPoliceVideo();
      return;
    }
    if (resultBox) resultBox.innerHTML = isVideo ? '正在抽帧识别，长视频会自动限量抽样...' : '正在识别，请稍候...';
    try {
      const endpoint = endpoints[module] + (isVideo && module === 'police' ? '?interval=1&max_results=300&max_sampled_frames=900' : '');
      const res = await fetch(endpoint, { method: 'POST', body: form, headers });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '识别失败');
      if (isVideo) this.renderVideoResult(module, data);
      else this.renderResult(module, data);
      if (module === 'owner' && data.action) this.loadVehicleState();
    } catch (e) {
      if (resultBox) resultBox.innerHTML = `识别失败：${e.message}`;
      alert(e.message);
    }
  },

  showUploadPreview(module, file, isVideo) {
    const imagePreview = document.getElementById(module + '-preview');
    const videoPreview = document.getElementById(module + '-upload-preview');
    const policeVideoControls = module === 'police' ? document.getElementById('police-upload-controls') : null;
    const policePlayButton = module === 'police' ? document.getElementById('police-upload-play') : null;
    const url = URL.createObjectURL(file);
    if (isVideo) {
      if (videoPreview) {
        videoPreview.pause();
        videoPreview.src = url;
        videoPreview.preload = 'auto';
        videoPreview.playsInline = true;
        videoPreview.controls = true;
        videoPreview.hidden = false;
        videoPreview.load();
      }
      if (policeVideoControls) {
        policeVideoControls.hidden = false;
      }
      if (policePlayButton) {
        policePlayButton.textContent = '播放视频';
      }
      if (imagePreview) {
        imagePreview.removeAttribute('src');
        imagePreview.hidden = true;
      }
      return;
    }

    if (videoPreview) {
      videoPreview.pause();
      videoPreview.removeAttribute('src');
      videoPreview.hidden = true;
    }
    if (policeVideoControls) {
      policeVideoControls.hidden = true;
    }
    if (imagePreview) {
      imagePreview.src = url;
      imagePreview.hidden = false;
    }
  },

  async toggleUploadedPolicePlayback() {
    const video = document.getElementById('police-upload-preview');
    const button = document.getElementById('police-upload-play');
    const resultBox = document.getElementById('police-result');
    if (!video || video.hidden || !video.src) return;
    try {
      if (video.paused || video.ended) {
        if (video.ended) video.currentTime = 0;
        await video.play();
      } else {
        video.pause();
      }
    } catch (e) {
      if (resultBox) resultBox.innerHTML = `视频播放失败：${e.message || e}`;
    } finally {
      if (button) button.textContent = video.paused ? '播放视频' : '暂停视频';
    }
  },

  showAnnotatedPreview(module, base64Image) {
    const imagePreview = document.getElementById(module + '-preview');
    if (!imagePreview || !base64Image) return;
    imagePreview.src = 'data:image/jpeg;base64,' + base64Image;
    imagePreview.hidden = false;
  },

  startUploadedPoliceVideo() {
    this.stopStream();
    const video = document.getElementById('police-upload-preview');
    const canvas = document.getElementById('police-canvas');
    const resultBox = document.getElementById('police-result');
    if (!video || !canvas) return;
    if (resultBox) resultBox.innerHTML = '播放视频后开始实时识别...';

    this.streamModule = 'police';
    this.streamBusy = false;
    this.uploadedRecognitionResults = [];
    const ctx = canvas.getContext('2d');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.wsStream = new WebSocket(`${proto}://${location.host}/ws/stream/police`);
    const sampleFps = 8;
    const sampleMs = 1000 / sampleFps;
    let processedFrames = 0;
    let lastSentAt = 0;
    let lastResultAt = -1;
    let waitingForFirstPlay = true;

    const renderSynchronizedResult = (row) => {
      if (!resultBox || !row) return;
      const now = Number.isFinite(video.currentTime) ? video.currentTime : row.time_sec;
      const lag = Math.max(0, now - row.time_sec);
      resultBox.innerHTML = `${row.gesture_cn}<br><small>confidence ${(row.confidence * 100).toFixed(0)}%</small><br><small>video ${now.toFixed(1)}s / label ${row.time_sec.toFixed(1)}s / lag ${lag.toFixed(1)}s</small>`;
    };

    const updateStatus = () => {
      if (!resultBox) return;
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      const current = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      resultBox.dataset.status = `sampled ${processedFrames} frames`;
      if (!this.uploadedRecognitionResults.length) {
        const status = waitingForFirstPlay ? '等待播放视频...' : `实时识别中，最高 ${sampleFps} FPS...`;
        resultBox.innerHTML = `${status}<br><small>${current.toFixed(1)}s / ${duration ? duration.toFixed(1) : '?'}s</small>`;
      }
    };

    const sendCurrentFrame = () => {
      if (!this.streamModule || this.wsStream?.readyState !== WebSocket.OPEN) return;
      if (video.paused || video.ended || video.readyState < 2 || this.streamBusy) return;
      const timeSec = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      if (timeSec <= lastResultAt && processedFrames > 0) return;
      this.streamBusy = true;
      updateStatus();
      canvas.width = video.videoWidth || 512;
      canvas.height = video.videoHeight || 512;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.72);
      this.wsStream.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1], time_sec: timeSec }));
      this.streamTimeout = setTimeout(() => {
        this.streamBusy = false;
      }, 5000);
    };

    const tick = () => {
      if (!this.streamModule) return;
      const now = performance.now();
      if (now - lastSentAt >= sampleMs) {
        lastSentAt = now;
        sendCurrentFrame();
      }
      if (!video.paused && !video.ended) updateStatus();
    };

    this.wsStream.onopen = () => {
      video.onplay = () => {
        waitingForFirstPlay = false;
        const button = document.getElementById('police-upload-play');
        if (button) button.textContent = '暂停视频';
        updateStatus();
      };
      video.onpause = () => {
        const button = document.getElementById('police-upload-play');
        if (button) button.textContent = '播放视频';
        if (resultBox) resultBox.dataset.status = 'paused';
      };
      video.onseeked = () => { lastResultAt = -1; updateStatus(); };
      this.streamInterval = setInterval(tick, 40);
      updateStatus();
    };
    this.wsStream.onmessage = (e) => {
      if (this.streamTimeout) {
        clearTimeout(this.streamTimeout);
        this.streamTimeout = null;
      }
      this.streamBusy = false;
      const msg = JSON.parse(e.data);
      if (msg.type === 'result') {
        processedFrames += 1;
        const row = { ...msg.data, time_sec: Number(msg.time_sec ?? msg.data?.time_sec ?? video.currentTime ?? 0) };
        this.uploadedRecognitionResults.push(row);
        lastResultAt = row.time_sec;
        renderSynchronizedResult(row);
      }
      if (msg.type === 'error' && resultBox) resultBox.innerHTML = `Video recognition error: ${msg.message}`;
    };
    this.wsStream.onerror = () => {
      if (resultBox) resultBox.innerHTML = 'Realtime video recognition connection failed';
    };
  },

  renderVideoResult(module, data) {
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const resultBox = document.getElementById(resultMap[module]);
    const results = data.results || [];
    const changes = data.changes || [];
    const best = results.filter(r => r.success).sort((a, b) => (b.confidence || 0) - (a.confidence || 0))[0] || results[results.length - 1] || results[0];
    const hitCount = data.result_count || 0;
    const timeline = (changes.length ? changes : results).slice(0, 20).map(r => {
      const t = r.time_sec == null ? `frame ${r.frame}` : `${Number(r.time_sec).toFixed(1)}s`;
      return `<div class="history-item"><span>${t}</span><span>${r.gesture_cn || r.gesture} ${(r.confidence*100).toFixed(0)}%</span></div>`;
    }).join('');
    const summary = `<div class="result-banner ${hitCount ? 'success' : 'danger'}">
      <div class="result-title">视频处理完成</div>
      <div class="result-subtitle">抽样 ${data.sampled_frames || 0} 帧，命中 ${hitCount} 个有效手势，记录 ${changes.length || results.length} 次变化</div>
    </div>
    <div class="history-list">${timeline || '<p>未识别到动作变化</p>'}</div>`;
    if (best) this.renderResult(module, best);
    if (resultBox) {
      if (best) resultBox.insertAdjacentHTML('afterbegin', summary);
      else resultBox.innerHTML = summary;
    }
  },

  renderResult(module, data) {
    if (module === 'lpr') {
      this.showAnnotatedPreview('lpr', data.annotated_image);
      document.getElementById('lpr-results').innerHTML = `
        <div class="result-banner ${data.success ? 'success' : 'danger'}">
          <div class="result-title">${data.success ? '识别成功' : '未识别到有效车牌'}</div>
          <div class="result-subtitle">共检测到 ${data.plate_count} 个车牌</div>
        </div>`;
      document.getElementById('lpr-plates').innerHTML = data.plates.map(p =>
        `<div class="plate-item"><span class="number">${p.plate_number}</span><span class="color">${p.plate_color} (${(p.confidence*100).toFixed(0)}%)</span></div>`
      ).join('') || '<p>未检测到车牌</p>';
      this.loadLprHistory();
    } else if (module === 'police') {
      this.showAnnotatedPreview('police', data.annotated_image);
      document.getElementById('police-result').innerHTML = `${data.gesture_cn}<br><small>置信度 ${(data.confidence*100).toFixed(0)}%</small>`;
      this.loadPoliceHistory();
    } else if (module === 'owner') {
      this.showAnnotatedPreview('owner', data.annotated_image);
      document.getElementById('owner-result').innerHTML = `${data.gesture_cn}${data.action ? '<br><small>→ ' + data.action + '</small>' : ''}`;
    }
  },

  async loadLprHistory() {
    try {
      const data = await this.api('/api/lpr/history?limit=10');
      document.getElementById('lpr-history').innerHTML = data.map(r =>
        `<div class="history-item"><span>#${r.id} · ${r.plate_count}个车牌</span><span>${new Date(r.created_at).toLocaleString()}</span></div>`
      ).join('') || '<p>暂无记录</p>';
    } catch (e) {}
  },

  async loadPoliceGestures() {
    try {
      const data = await this.api('/api/police-gesture/gestures');
      document.getElementById('police-gesture-list').innerHTML = data.map(g =>
        `<span class="gesture-tag">${g.cn}</span>`
      ).join('');
    } catch (e) {}
  },

  ensurePolicePoseBackendControl() {
    if (document.getElementById('police-pose-backend')) return;
    const streamUrlInput = document.getElementById('police-stream-url');
    if (!streamUrlInput) return;
    const row = document.createElement('div');
    row.className = 'stream-url-row';
    row.innerHTML = `
      <select id="police-pose-backend">
        <option value="ctpgr">CTPGR Pose</option>
        <option value="yolo">YOLO-Pose</option>
      </select>
      <button class="btn" onclick="App.setPolicePoseBackend()">切换模型</button>
      <span id="police-pose-backend-status" style="color:var(--text-muted);align-self:center"></span>
    `;
    streamUrlInput.closest('.stream-url-row')?.before(row);
  },

  async loadPolicePoseBackend() {
    this.ensurePolicePoseBackendControl();
    try {
      const data = await this.api('/api/police-gesture/pose-backend');
      const select = document.getElementById('police-pose-backend');
      const status = document.getElementById('police-pose-backend-status');
      if (select) select.value = data.backend || 'ctpgr';
      if (status) status.textContent = data.backend === 'yolo' ? 'YOLO experimental' : 'stable';
    } catch (e) {}
  },

  async setPolicePoseBackend() {
    const select = document.getElementById('police-pose-backend');
    const status = document.getElementById('police-pose-backend-status');
    const backend = select?.value || 'ctpgr';
    this.stopStream();
    if (status) status.textContent = 'switching...';
    try {
      const data = await this.api('/api/police-gesture/pose-backend', {
        method: 'PUT',
        body: JSON.stringify({ backend }),
      });
      if (select) select.value = data.backend;
      if (status) status.textContent = data.backend === 'yolo' ? 'YOLO experimental' : 'stable';
      const resultBox = document.getElementById('police-result');
      if (resultBox) resultBox.innerHTML = `当前模型：${data.backend === 'yolo' ? 'YOLO-Pose' : 'CTPGR Pose'}`;
    } catch (e) {
      if (status) status.textContent = 'failed';
      alert(e.message);
      this.loadPolicePoseBackend();
    }
  },

  async loadPoliceHistory() {
    try {
      const data = await this.api('/api/police-gesture/history?limit=10');
      document.getElementById('police-history').innerHTML = data.map(r =>
        `<div class="history-item"><span>${r.gesture_cn}</span><span>${(r.confidence*100).toFixed(0)}%</span></div>`
      ).join('');
    } catch (e) {}
  },

  async loadOwnerGestures() {
    try {
      const data = await this.api('/api/owner-gesture/gestures');
      document.getElementById('owner-gestures').innerHTML = data.map(g =>
        `<span class="gesture-tag">${g.cn} → ${g.action || '-'}</span>`
      ).join('');
    } catch (e) {}
  },

  async loadVehicleState() {
    try {
      const s = await this.api('/api/owner-gesture/vehicle-state');
      document.getElementById('v-awake').textContent = s.is_awake ? '已唤醒' : '休眠';
      document.getElementById('v-page').textContent = s.current_page;
      document.getElementById('v-volume').value = s.volume;
      document.getElementById('v-volume-val').textContent = s.volume;
      document.getElementById('v-temp').value = s.temperature;
      document.getElementById('v-temp-val').textContent = s.temperature;
      document.getElementById('v-phone').textContent = s.phone_status === 'in_call' ? '通话中' : '空闲';
    } catch (e) {}
  },

  async updateVehicle() {
    const data = {
      volume: +document.getElementById('v-volume').value,
      temperature: +document.getElementById('v-temp').value,
      phone_status: document.getElementById('v-phone').textContent === '通话中' ? 'in_call' : 'idle',
      current_page: document.getElementById('v-page').textContent,
      is_awake: document.getElementById('v-awake').textContent === '已唤醒' ? 1 : 0,
    };
    document.getElementById('v-volume-val').textContent = data.volume;
    document.getElementById('v-temp-val').textContent = data.temperature;
    try {
      await this.api('/api/owner-gesture/vehicle-state', { method: 'PUT', body: JSON.stringify(data) });
    } catch (e) {}
  },

  setPhone(status) {
    document.getElementById('v-phone').textContent = status === 'in_call' ? '通话中' : '空闲';
    this.updateVehicle();
  },

  async startStream(module) {
    this.stopStream();
    this.streamModule = module;
    const video = document.getElementById(module + '-video');
    const canvas = document.getElementById(module + '-canvas');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      video.srcObject = stream;
      video.hidden = false;
      canvas.hidden = false;
      const ctx = canvas.getContext('2d');

      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const openStreamSocket = () => {
        if (!this.streamModule) return;
        this.wsStream = new WebSocket(`${proto}://${location.host}/ws/stream/${module}`);
        this.wsStream.onmessage = (e) => {
          if (this.streamTimeout) {
            clearTimeout(this.streamTimeout);
            this.streamTimeout = null;
          }
          this.streamBusy = false;
          const msg = JSON.parse(e.data);
          if (msg.type === 'result') this.renderResult(module, msg.data);
        };
        this.wsStream.onclose = () => {
          if (this.streamTimeout) {
            clearTimeout(this.streamTimeout);
            this.streamTimeout = null;
          }
          this.streamBusy = false;
        };
      };
      openStreamSocket();

      this.streamInterval = setInterval(() => {
        if (!this.streamModule) return;
        if (!this.wsStream || this.wsStream.readyState === WebSocket.CLOSED) openStreamSocket();
        if (video.readyState >= 2 && this.wsStream?.readyState === WebSocket.OPEN && !this.streamBusy) {
          this.streamBusy = true;
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0);
          const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
          this.wsStream.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1] }));
          this.streamTimeout = setTimeout(() => {
            this.streamBusy = false;
            if (this.wsStream) this.wsStream.close();
            this.wsStream = null;
          }, 10000);
        }
      }, 125);
    } catch (e) { alert('无法访问摄像头: ' + e.message); }
  },

  startUrlStream(module) {
    this.stopStream();
    const input = document.getElementById(module + '-stream-url');
    const url = (input?.value || '').trim();
    if (!url) {
      alert('请输入 rtsp/http 视频流地址');
      return;
    }

    this.streamModule = module;
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const resultBox = document.getElementById(resultMap[module]);
    if (resultBox) resultBox.innerHTML = '正在连接视频流...';

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.wsStream = new WebSocket(`${proto}://${location.host}/ws/stream-url/${module}`);
    this.wsStream.onopen = () => {
      this.wsStream.send(JSON.stringify({ type: 'start', url, interval: 1, target_fps: 8 }));
    };
    this.wsStream.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'status' && resultBox) resultBox.innerHTML = '视频流已连接，正在识别...';
      if (msg.type === 'result') this.renderResult(module, msg.data);
      if (msg.type === 'error') {
        if (resultBox) resultBox.innerHTML = `视频流错误：${msg.message}`;
        else alert(msg.message);
      }
    };
    this.wsStream.onerror = () => {
      if (resultBox) resultBox.innerHTML = '视频流连接失败';
    };
  },

  stopStream() {
    if (this.streamInterval) { clearInterval(this.streamInterval); this.streamInterval = null; }
    if (this.streamTimeout) { clearTimeout(this.streamTimeout); this.streamTimeout = null; }
    if (this.uploadedLabelInterval) { clearInterval(this.uploadedLabelInterval); this.uploadedLabelInterval = null; }
    if (this.uploadedRecognitionVideo) {
      this.uploadedRecognitionVideo.pause();
      this.uploadedRecognitionVideo.removeAttribute('src');
      this.uploadedRecognitionVideo.load();
      this.uploadedRecognitionVideo = null;
    }
    this.uploadedRecognitionResults = [];
    if (this.wsStream) { this.wsStream.close(); this.wsStream = null; }
    this.streamBusy = false;
    if (this.streamModule) {
      const video = document.getElementById(this.streamModule + '-video');
      if (video?.srcObject) { video.srcObject.getTracks().forEach(t => t.stop()); video.srcObject = null; }
      if (video) video.hidden = true;
      this.streamModule = null;
    }
  },

  async loadAlerts() {
    try {
      const [stats, alerts] = await Promise.all([
        this.api('/api/monitor/alerts/stats'),
        this.api('/api/monitor/alerts?limit=30'),
      ]);
      document.getElementById('alert-stats').innerHTML = Object.entries(stats.by_level || {}).map(([k, v]) =>
        `<div class="stat-card"><div class="stat-num">${v}</div><div class="stat-label">${k}</div></div>`
      ).join('');
      document.getElementById('alert-timeline').innerHTML = alerts.map(a =>
        `<div class="timeline-item ${a.level}"><strong>${a.title}</strong><br>${a.summary}<br><small>${new Date(a.created_at).toLocaleString()}</small>${a.suggestion ? '<br><em>建议: ' + a.suggestion + '</em>' : ''}</div>`
      ).join('') || '<p>暂无告警</p>';
    } catch (e) {}
  },

  async testAlert() {
    try {
      const data = await this.api('/api/monitor/alerts/test', { method: 'POST' });
      this.showToast({ level: 'info', title: data.title, summary: data.summary });
      this.loadAlerts();
    } catch (e) { alert(e.message); }
  },

  async loadLogs() {
    const cat = document.getElementById('log-category')?.value || '';
    try {
      const data = await this.api('/api/monitor/logs?limit=50' + (cat ? '&category=' + cat : ''));
      document.getElementById('log-table').innerHTML =
        '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span></div>' +
        data.map(l =>
          `<div class="log-row"><span>${new Date(l.created_at).toLocaleString()}</span><span class="level-${l.level}">${l.level}</span><span>${l.category}</span><span>${l.message}</span></div>`
        ).join('') || '<p>暂无日志</p>';
    } catch (e) {
      document.getElementById('log-table').innerHTML = `<p>加载日志失败: ${e.message}</p>`;
    }
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
