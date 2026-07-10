const App = {
  token: localStorage.getItem('token') || '',
  streamModule: null,
  streamInterval: null,
  wsAlerts: null,
  wsStream: null,
  lprVideoWs: null,
  lprVideoBusy: false,
  lprVideoTimer: null,
  lprVideoMode: null,
  lprRtspTimer: null,
  lprRtspBusy: false,
  lprRtspWs: null,
  lprRtspCapture: null,
  lprRtspMode: null,
  lprRtspStartedAt: null,
  lprRtspSourceName: null,
  lprRtspHls: null,
  lprRtspRetryTimer: null,

  init() {
    this.bindTabs();
    this.bindNav();
    this.bindFileInputs();
    this.bindLprDragDrop();
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
    document.getElementById('lpr-file').onchange = (e) => this.handleLprInput(e.target.files[0]);
    document.getElementById('police-file').onchange = (e) => this.uploadFile('police', e.target.files[0]);
    document.getElementById('owner-file').onchange = (e) => this.uploadFile('owner', e.target.files[0]);
  },

  bindLprDragDrop() {
    const zone = document.getElementById('lpr-upload');
    if (!zone) return;
    ['dragenter', 'dragover'].forEach(evt => {
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add('drag-over'); });
    });
    ['dragleave', 'drop'].forEach(evt => {
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove('drag-over'); });
    });
    zone.addEventListener('drop', (e) => {
      const file = e.dataTransfer?.files?.[0];
      if (file) this.handleLprInput(file);
    });
  },

  formatPlateNumber(num) {
    if (!num || num.length < 7) return num || '--';
    return num.slice(0, 2) + '·' + num.slice(2);
  },

  plateColorClass(color) {
    return { '蓝牌': 'plate-blue', '绿牌': 'plate-green', '黄牌': 'plate-yellow',
             '白牌': 'plate-white', '黑牌': 'plate-black' }[color] || '';
  },

  lprSourceLabel(data) {
    const src = data?.source || data?.plates?.[0]?.source;
    if (src === 'ccpd_gt') return 'CCPD 文件名标注';
    if (src === 'model') return 'RPNet 模型检测';
    if (src === 'yolo_lprnet') return 'YOLO+LPRNet 视频检测';
    return data?.model_available === false ? '模型未加载' : '自动识别';
  },

  lprEngineLabel(data) {
    const src = data?.source || data?.plates?.[0]?.source;
    if (src === 'yolo_lprnet') return 'runtime_api / yolo_lprnet_assets';
    if (src === 'ccpd_gt') return 'backend / CCPD';
    if (src === 'model') return 'backend / RPNet';
    return 'backend';
  },

  async loadLprModelStatus() {
    try {
      const [imgSt, vidSt] = await Promise.all([
        this.api('/api/lpr/model-status'),
        this.api('/api/lpr/video-model-status'),
      ]);
      const el = document.getElementById('lpr-model-status');
      if (el) {
        el.textContent = imgSt.model_available
          ? '图片识别：RPNet 已就绪'
          : `图片识别：${imgSt.message || '模型未加载'}`;
        el.className = imgSt.model_available ? 'section-desc model-ok' : 'section-desc model-warn';
      }
      const vel = document.getElementById('lpr-video-model-status');
      if (vel) {
        vel.textContent = vidSt.model_available
          ? '视频识别：YOLO+LPRNet 已就绪（支持多车牌）'
          : `视频识别：${vidSt.message || '模型未加载'}`;
        vel.className = vidSt.model_available ? 'section-desc model-ok' : 'section-desc model-warn';
      }
    } catch (e) {}
  },

  setLprLoading() {
    const el = document.getElementById('lpr-loading');
    if (el) el.classList.add('hidden');
  },

  onViewChange(view) {
    if (view === 'dashboard') this.loadDashboard();
    if (view === 'lpr') { this.loadLprHistory(); this.loadLprModelStatus(); }
    if (view === 'police') { this.loadPoliceGestures(); this.loadPoliceHistory(); }
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
    this.stopVideoStream();
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
      const [lprStats, police, owner, stats] = await Promise.all([
        this.api('/api/lpr/stats'),
        this.api('/api/police-gesture/history?limit=100'),
        this.api('/api/owner-gesture/history?limit=100'),
        this.api('/api/monitor/alerts/stats'),
      ]);
      document.getElementById('stat-lpr').textContent = lprStats.total ?? 0;
      document.getElementById('stat-police').textContent = police.length;
      document.getElementById('stat-owner').textContent = owner.length;
      document.getElementById('stat-alerts').textContent = stats.total;
      const el = document.getElementById('dashboard-alerts');
      el.innerHTML = stats.recent.slice(0, 5).map(a =>
        `<div class="alert-item ${a.level}"><div class="alert-title">${a.title}</div><div>${a.summary}</div></div>`
      ).join('') || '<p style="color:var(--text-muted)">暂无告警</p>';
    } catch (e) { console.error(e); }
  },

  isCcpdFilename(name) {
    return /^.+-.+-.+-.+-.+-.+-.+$/i.test((name || '').replace(/\\/g, '/').split('/').pop().replace(/\.[^.]+$/, ''));
  },

  isVideoFile(file) {
    const name = (file?.name || '').toLowerCase();
    const type = (file?.type || '').toLowerCase();
    return type.startsWith('video/') || /\.(mp4|webm|mov|mkv|avi|m4v)$/i.test(name);
  },

  async handleLprInput(file) {
    if (!file) return;
    this.clearLprDisplay();
    if (this.isVideoFile(file)) {
      this.setLprLoading(true, { forceHide: true });
      await this.startVideoFileStream(file);
      return;
    }
    const isCcpd = this.isCcpdFilename(file.name);
    await this.uploadFile('lpr', file, { forceModel: !isCcpd, ccpd: isCcpd });
  },

  async uploadFile(module, file, options = {}) {
    if (!file) return;
    if (module === 'lpr') this.clearLprDisplay();
    const endpoints = { lpr: '/api/lpr/recognize', police: '/api/police-gesture/recognize', owner: '/api/owner-gesture/recognize' };
    const previewMap = { lpr: 'lpr-preview', police: 'police-preview', owner: 'owner-preview' };
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const preview = document.getElementById(previewMap[module]);
    const resultBox = document.getElementById(resultMap[module]);
    if (preview && file.type.startsWith('image/')) preview.src = URL.createObjectURL(file);
    if (resultBox) resultBox.innerHTML = '<div class="result-banner"><div class="result-title">正在识别，请稍候…</div></div>';
    if (module === 'lpr') this.setLprLoading(true);

    const headers = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;

    try {
      if (module === 'lpr' && file.type.startsWith('video/')) {
        await this.startVideoFileStream(file);
        return;
      }

      let data;
      {
        const form = new FormData();
        form.append('file', file);
        const url = module === 'lpr'
          ? `${endpoints[module]}?mode=${options.forceModel ? 'lprnet' : 'ccpd'}`
          : endpoints[module];
        const res = await fetch(url, { method: 'POST', body: form, headers });
        data = await res.json();
        if (!res.ok) throw new Error(data.detail || '识别失败');
      }
      this.renderResult(module, data);
      if (module === 'owner' && data.action) this.loadVehicleState();
    } catch (e) {
      if (resultBox) resultBox.innerHTML = `<div class="result-banner danger"><div class="result-title">识别失败</div><div class="result-subtitle">${e.message}</div></div>`;
      alert(e.message);
    } finally {
      if (module === 'lpr') this.setLprLoading(false);
    }
  },

  renderResult(module, data, opts = {}) {
    if (module === 'lpr') {
      const isVideo = opts.video === true;
      const resultBoxId = isVideo ? 'lpr-video-result' : 'lpr-image-result';
      const plateTarget = isVideo ? 'lpr-video-plates' : 'lpr-plates';
      const statusEl = document.getElementById('lpr-video-model-status');
      if (statusEl && isVideo) {
        statusEl.textContent = `${this.lprEngineLabel(data)} · ${data.model_available ? '已连接' : '未加载'}`;
      }
      const fileVideo = document.getElementById('lpr-video-output');
      const camVideo = document.getElementById('lpr-video');
      const canvas = document.getElementById('lpr-canvas');
      const preview = document.getElementById('lpr-preview');
      if (!isVideo && data.annotated_image && preview) {
        preview.src = 'data:image/jpeg;base64,' + data.annotated_image;
      }
      if (isVideo) {
        if (preview) preview.removeAttribute('src');
        if (fileVideo) fileVideo.hidden = true;
        if (camVideo) camVideo.hidden = true;
        if (canvas) canvas.hidden = true;
        const imgResult = document.getElementById('lpr-image-result');
        if (imgResult) imgResult.innerHTML = '';
      } else {
        if (fileVideo) fileVideo.hidden = true;
        if (camVideo) camVideo.hidden = true;
        if (canvas) canvas.hidden = true;
      }
      const plateSummary = (data.plates || []).map(p => p.plate_number).filter(Boolean).join('、') || '';
      const failMsg = isVideo
        ? (data.model_available === false
          ? 'YOLO+LPRNet 未加载，请将权重放到 vehicle-vision-system/yolo_lprnet_assets/weights/ 或 backend/weights/'
          : '当前帧未检测到有效车牌')
        : (data.model_available === false
          ? 'RPNet 模型未加载，请将 fh02.pth 放到 backend/app/models/'
          : '未识别到有效车牌，请使用 CCPD 数据集图片（文件名含标注）');

      document.getElementById(resultBoxId).innerHTML = `
        <div class="result-banner ${data.success ? 'success' : 'danger'}">
          <div class="result-title">${data.success
            ? (isVideo ? `✓ 检测到 ${data.plate_count} 个车牌` : '✓ 识别成功')
            : (isVideo ? '○ 实时识别中…' : '✗ 未识别到有效车牌')}</div>
          <div class="result-subtitle">${data.success
            ? `${plateSummary}${isVideo ? ' · ' + this.lprSourceLabel(data) : ''}`
            : failMsg}</div>
          <div class="result-subtitle">${this.lprEngineLabel(data)}</div>
        </div>`;

      const hero = document.getElementById('lpr-hero');
      const main = data.plates?.[0];
      if (!isVideo && main) {
        hero.classList.remove('hidden');
        document.getElementById('lpr-hero-plate').textContent = this.formatPlateNumber(main.plate_number);
        const cls = this.plateColorClass(main.plate_color);
        document.getElementById('lpr-hero-meta').innerHTML =
          `<span class="plate-badge ${cls}">${main.plate_color || '蓝牌'}</span> ${this.lprSourceLabel(data)} · 置信度 ${((main.confidence || 0) * 100).toFixed(0)}%`;
        const fill = hero.querySelector('.hero-conf-fill');
        if (fill) fill.style.width = `${Math.min(100, (main.confidence || 0) * 100)}%`;
      } else {
        hero.classList.add('hidden');
      }

      document.getElementById(plateTarget).innerHTML = (data.plates || []).map(p =>
        `<div class="plate-item">
          <span class="number">${this.formatPlateNumber(p.plate_number)}</span>
          <span class="color ${this.plateColorClass(p.plate_color)}">${p.plate_color || '蓝牌'}</span>
          ${isVideo ? `<span class="history-meta" style="margin-left:.5rem">${((p.confidence || 0) * 100).toFixed(0)}%</span>` : ''}
        </div>`
      ).join('') || '<p style="color:var(--text-muted)">未检测到车牌</p>';
      if (!opts.skipHistory && !isVideo) {
        this.loadLprHistory();
        this.loadDashboard();
      }
      if (isVideo && data.success) {
        this.loadLprHistory();
        this.loadDashboard();
      }
    } else if (module === 'police') {
      document.getElementById('police-preview').src = 'data:image/jpeg;base64,' + data.annotated_image;
      document.getElementById('police-result').innerHTML = `${data.gesture_cn}<br><small>置信度 ${(data.confidence*100).toFixed(0)}%</small>`;
      this.loadPoliceHistory();
    } else if (module === 'owner') {
      document.getElementById('owner-preview').src = 'data:image/jpeg;base64,' + data.annotated_image;
      document.getElementById('owner-result').innerHTML = `${data.gesture_cn}${data.action ? '<br><small>→ ' + data.action + '</small>' : ''}`;
    }
  },

  async loadLprHistory() {
    try {
      const data = await this.api('/api/lpr/history?limit=10');
      document.getElementById('lpr-history').innerHTML = data.map(r => {
        const plates = (r.plates || []);
        const first = plates[0];
        const summary = plates.map(p => this.formatPlateNumber(p.plate_number)).filter(Boolean).join('、') || '未识别';
        return `<div class="history-item" onclick="App.showHistoryRecord(${r.id})" data-id="${r.id}">
          <div>
            <span class="history-plate">${summary}</span>
            ${first ? `<span class="plate-badge ${this.plateColorClass(first.plate_color)}" style="font-size:.75rem;margin-left:.5rem">${first.plate_color || '蓝牌'}</span>` : ''}
            <div class="history-meta">#${r.id} · ${r.plate_count}个车牌 · ${r.source_type || 'image'}</div>
          </div>
          <span class="history-meta">${new Date(r.created_at).toLocaleString()}</span>
        </div>`;
      }).join('') || '<p style="color:var(--text-muted)">暂无记录</p>';
      this._lprHistoryCache = data;
    } catch (e) {}
  },

  showHistoryRecord(id) {
    const rec = (this._lprHistoryCache || []).find(r => r.id === id);
    if (!rec || !rec.annotated_image) return;
    document.getElementById('lpr-preview').src = 'data:image/jpeg;base64,' + rec.annotated_image;
    this.renderResult('lpr', {
      success: rec.plate_count > 0,
      plate_count: rec.plate_count,
      plates: rec.plates || [],
      annotated_image: rec.annotated_image,
    });
  },

  async loadCcpdSamples() {
    const container = document.getElementById('lpr-ccpd-samples');
    container.classList.remove('hidden');
    container.innerHTML = '<p style="padding:.5rem;color:var(--text-muted)">加载 CCPD 样本列表…</p>';
    try {
      const data = await this.api('/api/lpr/ccpd-sample');
      if (!data.samples?.length) {
        container.innerHTML = `<p style="padding:.5rem;color:var(--text-muted)">${data.message || '暂无样本，请配置 CCPD 数据集'}</p>`;
        return;
      }
      container.innerHTML = data.samples.map(s =>
        `<div class="ccpd-sample-item" onclick="App.recognizeCcpdSample('${s.relative.replace(/'/g, "\\'")}')">
          <span title="${s.relative}">${s.relative.split('/').pop()}</span>
          <span class="${s.exists ? 'exists-yes' : 'exists-no'}">${s.exists ? '可识别' : '文件缺失'}</span>
        </div>`
      ).join('');
    } catch (e) {
      container.innerHTML = `<p style="padding:.5rem;color:var(--danger)">加载失败: ${e.message}</p>`;
    }
  },

  async recognizeCcpdSample(relative) {
    this.setLprLoading(true);
    document.getElementById('lpr-results').innerHTML =
      '<div class="result-banner"><div class="result-title">正在识别 CCPD 样本…</div></div>';
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
      const res = await fetch(`/api/lpr/recognize-ccpd?relative=${encodeURIComponent(relative)}`, {
        method: 'POST', headers,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '识别失败');
      this.renderResult('lpr', data);
    } catch (e) {
      document.getElementById('lpr-results').innerHTML =
        `<div class="result-banner danger"><div class="result-title">识别失败</div><div class="result-subtitle">${e.message}</div></div>`;
      alert(e.message);
    } finally {
      this.setLprLoading(false);
    }
  },

  async loadPoliceGestures() {
    try {
      const data = await this.api('/api/police-gesture/gestures');
      document.getElementById('police-gesture-list').innerHTML = data.map(g =>
        `<span class="gesture-tag">${g.cn}</span>`
      ).join('');
    } catch (e) {}
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
    if (module === 'lpr') {
      this.stopVideoStream();
      this.lprVideoMode = 'camera';
      const video = document.getElementById('lpr-video');
      const canvas = document.getElementById('lpr-canvas');
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        video.srcObject = stream;
        video.controls = false;
        video.hidden = false;
        const fileVideo = document.getElementById('lpr-video-output');
        if (fileVideo) fileVideo.hidden = true;
        if (canvas) canvas.hidden = false;
        document.getElementById('lpr-results').innerHTML =
          '<div class="result-banner"><div class="result-title">摄像头实时识别中…</div></div>';
        await this.connectLprVideoWs();
        this.lprVideoTimer = setInterval(() => this.captureAndSendLprFrame(video, canvas), 350);
      } catch (e) { alert('无法访问摄像头: ' + e.message); }
      return;
    }

    this.stopStream();
    this.streamModule = module;
    const video = document.getElementById(module + '-video');
    const canvas = document.getElementById(module + '-canvas');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      video.srcObject = stream;
      video.hidden = false;
      canvas.hidden = true;
      const ctx = canvas.getContext('2d');
      const statusEl = document.getElementById('lpr-video-model-status');
      if (statusEl) statusEl.textContent = '摄像头已打开，等待识别结果…';

      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.wsStream = new WebSocket(`${proto}://${location.host}/ws/stream/${module}`);
      this.wsStream.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'result') this.renderResult(module, msg.data);
      };

      this.streamInterval = setInterval(() => {
        if (video.readyState >= 2 && this.wsStream?.readyState === WebSocket.OPEN) {
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0);
          const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
          this.wsStream.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1] }));
        }
      }, 500);
    } catch (e) { alert('无法访问摄像头: ' + e.message); }
  },

  clearLprDisplay() {
    const preview = document.getElementById('lpr-preview');
    const videoResult = document.getElementById('lpr-video-result');
    const plateTarget = document.getElementById('lpr-video-plates');
    const imgResult = document.getElementById('lpr-image-result');
    const hero = document.getElementById('lpr-hero');
    if (preview) preview.removeAttribute('src');
    if (videoResult) videoResult.innerHTML = '';
    if (plateTarget) plateTarget.innerHTML = '';
    if (imgResult) imgResult.innerHTML = '';
    if (hero) hero.classList.add('hidden');
  },

  syncLprRtspUrl() {
    const urlInput = document.getElementById('lpr-rtsp-url');
    const sourceSelect = document.getElementById('lpr-rtsp-source');
    if (urlInput && sourceSelect?.value) urlInput.value = sourceSelect.value;
  },

  setRtspVideo(url) {
    const video = document.getElementById('lpr-rtsp-video');
    const player = document.getElementById('lpr-rtsp-player');
    const debug = document.getElementById('lpr-rtsp-debug');
    if (!video || !player || !url) return;
    player.classList.remove('hidden');
    if (debug) debug.textContent = `准备加载：${url}`;
    video.src = url;
    const update = (txt) => { if (debug) debug.textContent = txt; };
    video.onload = () => update(`已加载：${url}`);
    video.onerror = () => update(`预览错误：${video.error?.message || video.error?.code || 'unknown'}`);
  },

  async startLprRtspStream() {
    this.stopVideoStream();
    this.lprVideoMode = 'rtsp';
    this.lprRtspMode = 'rtsp';
    const urlInput = document.getElementById('lpr-rtsp-url');
    const sourceSelect = document.getElementById('lpr-rtsp-source');
    const rtspUrl = (urlInput?.value || '').trim();
    const source = sourceSelect?.value || 'rtsp://10.126.59.120:8554/live/live1';
    const presetLabel = sourceSelect?.selectedOptions?.[0]?.textContent?.trim() || '';
    if (urlInput) urlInput.readOnly = false;
    const statusEl = document.getElementById('lpr-rtsp-status');
    const progress = document.getElementById('lpr-rtsp-progress');
    const progressFill = document.getElementById('lpr-rtsp-progress-fill');
    const progressText = document.getElementById('lpr-rtsp-progress-text');
    const resultBox = document.getElementById('lpr-rtsp-result');
    if (!rtspUrl) {
      alert('请输入 RTSP 地址');
      return;
    }
    if (statusEl) statusEl.textContent = `准备连接 ${presetLabel || source} · ${rtspUrl}`;
    if (progress) progress.classList.remove('hidden');
    if (progressFill) progressFill.style.width = '5%';
    if (progressText) progressText.textContent = '正在建立 RTSP 识别连接…';
    if (resultBox) resultBox.innerHTML = '<div class="result-banner"><div class="result-title">等待 RTSP 视频流…</div></div>';
    this.lprRtspStartedAt = Date.now();
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
      const localFile = /\.(mp4|avi|mov|mkv|m4v)$/i.test(rtspUrl) || /^(?:[a-zA-Z]:\\|file:\/\/)/.test(rtspUrl);
      const payload = localFile
        ? { rtsp_url: rtspUrl, source_name: source.split('/').pop() || 'live1', label: presetLabel || null }
        : { rtsp_url: rtspUrl, source_name: source.split('/').pop() || 'live1', label: presetLabel || null };
      const res = await fetch('/api/lpr/rtsp/start', {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'RTSP 启动失败');
      this.renderResult('lpr', data, { video: true, skipHistory: true });
      const previewUrl = data.preview_url || '';
      this.lprRtspSourceName = data.source_name || 'live1';
      if (previewUrl) {
        console.log('[RTSP] use preview url:', previewUrl);
        this.setRtspVideo(previewUrl);
        if (this.lprPreviewPoll) clearInterval(this.lprPreviewPoll);
        this.lprPreviewPoll = setInterval(async () => {
          try {
            const status = await this.api(`/api/lpr/preview/${this.lprRtspSourceName}/status`);
            const debug = document.getElementById('lpr-rtsp-debug');
            const player = document.getElementById('lpr-rtsp-player');
            if (debug) {
              const names = (status.plates || []).map(p => p.plate_number).filter(Boolean).join('、') || '暂无';
              debug.textContent = `帧 ${status.frame_index ?? 0} · 车牌 ${names}`;
            }
            if (player) player.classList.remove('hidden');
            const plateTarget = document.getElementById('lpr-rtsp-result');
            if (plateTarget) {
              plateTarget.innerHTML = (status.plates || []).map(p => `<div class="plate-item"><span class="number">${this.formatPlateNumber(p.plate_number)}</span><span class="color ${this.plateColorClass(p.plate_color)}">${p.plate_color || '蓝牌'}</span><span class="history-meta" style="margin-left:.5rem">${((p.confidence || 0) * 100).toFixed(0)}%</span></div>`).join('') || '<p style="color:var(--text-muted)">未检测到车牌</p>';
            }
            if (!status.running && this.lprPreviewPoll) {
              clearInterval(this.lprPreviewPoll);
              this.lprPreviewPoll = null;
            }
          } catch (err) {
            console.warn('[PREVIEW-STATUS]', err);
          }
        }, 1000);
      }
      if (statusEl) statusEl.textContent = data.message || `RTSP 识别已启动：${presetLabel || source}`;
      if (progressText) progressText.textContent = 'RTSP 识别已启动，正在等待实时结果…';
      if (progressFill) progressFill.style.width = '20%';
    } catch (e) {
      if (statusEl) statusEl.textContent = `RTSP 启动失败：${e.message}`;
      if (resultBox) resultBox.innerHTML = `<div class="result-banner danger"><div class="result-title">RTSP 启动失败</div><div class="result-subtitle">${e.message}</div></div>`;
      alert(e.message);
    }
  },

  async startVideoFileStream(file) {
    this.stopVideoStream();
    this.lprVideoMode = 'file';
    const video = document.getElementById('lpr-video-output');
    const canvas = document.getElementById('lpr-canvas');
    const progress = document.getElementById('lpr-video-progress');
    const progressFill = document.getElementById('lpr-video-progress-fill');
    const progressText = document.getElementById('lpr-video-progress-text');
    const previewImg = document.getElementById('lpr-preview');
    const imageResult = document.getElementById('lpr-image-result');
    const videoResult = document.getElementById('lpr-video-result');

    const debug = (...args) => console.log('[LPR-VIDEO]', ...args);
    debug('startVideoFileStream file=', file?.name, file?.type, file?.size);

    if (previewImg) previewImg.removeAttribute('src');
    if (imageResult) imageResult.innerHTML = '';
    if (document.getElementById('lpr-loading')) document.getElementById('lpr-loading').classList.add('hidden');
    const loadingEl = document.getElementById('lpr-loading');
    if (loadingEl) loadingEl.classList.add('hidden');
    video.hidden = false;
    video.controls = true;
    video.muted = true;
    video.playsInline = true;
    video.autoplay = false;
    video.src = URL.createObjectURL(file);
    video.load();
    debug('video src assigned', video.currentSrc || video.src);
    if (canvas) canvas.hidden = true;
    if (progress) progress.classList.remove('hidden');
    if (progressFill) progressFill.style.width = '5%';
    if (progressText) progressText.textContent = '视频已加载，等待浏览器播放并初始化实时识别…';
    if (videoResult) videoResult.innerHTML = '';
    this.setLprLoading(true);

    const statusEl = document.getElementById('lpr-video-model-status');
    if (statusEl) statusEl.textContent = '视频已选择，准备播放并实时识别…';

    const preview = document.getElementById('lpr-preview');
    const plateTarget = document.getElementById('lpr-video-plates');
    const playbackTools = document.getElementById('lpr-video-playback-tools');
    const speedSelect = document.getElementById('lpr-video-speed');
    if (playbackTools) playbackTools.classList.remove('hidden');
    if (speedSelect) {
      speedSelect.onchange = () => { video.playbackRate = Number(speedSelect.value || 1); debug('playbackRate set', video.playbackRate); };
      video.playbackRate = Number(speedSelect.value || 1);
    }
    let frameCount = 0;
    let sentCount = 0;
    const seenVideoPlates = [];
    const seenPlateKeys = new Set();

    try {
      debug('connecting websocket');
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.lprVideoWs = new WebSocket(`${proto}://${location.host}/ws/stream/lpr`);
      this.lprVideoWs.onopen = async () => {
        debug('websocket open');
          if (progressText) progressText.textContent = '识别连接已建立，正在尝试播放视频…';
        if (statusEl) statusEl.textContent = 'WebSocket 已连接，等待视频播放…';
        try {
          await video.play();
          debug('video.play() resolved');
        } catch (err) {
          debug('video.play() failed', err);
          if (progressText) progressText.textContent = '视频播放被浏览器拦截，请手动点播放';
          if (statusEl) statusEl.textContent = '视频播放被浏览器拦截，请点击视频播放按钮';
        }
      };
      this.lprVideoWs.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === 'error') {
          this.lprVideoBusy = false;
          debug('ws error message', msg.message);
          if (progressText) progressText.textContent = '识别失败: ' + msg.message;
          if (videoResult) videoResult.innerHTML = `<div class="result-banner danger"><div class="result-title">视频识别失败</div><div class="result-subtitle">${msg.message}</div></div>`;
          return;
        }
        if (msg.type === 'done') {
          debug('ws done', msg.plates, 'frames=', msg.frames, 'raw_valid=', msg.raw_valid, 'record_id=', msg.record_id, 'error=', msg.error);
          if (msg.module === 'lpr' && Array.isArray(msg.plates) && msg.plates.length) {
            const text = msg.plates.map(p => `${p.plate_number}(${((p.confidence || 0) * 100).toFixed(0)}%)`).join('、');
            console.log('[LPR-VIDEO] final saved plates', text);
            if (progressText) progressText.textContent = '视频识别完成，历史记录已保存';
            this.loadLprHistory();
            this.loadDashboard();
          } else if (progressText) {
            progressText.textContent = `视频识别完成，未保存有效历史记录（frames=${msg.frames || 0}, raw=${msg.raw_valid || 0}）`;
          }
          return;
        }
        if (msg.type === 'result') {
          this.lprVideoBusy = false;
          const data = msg.data || {};
          frameCount += 1;
          debug('frame result', data.frame, data.plate_count, data.plates);
          if (data.annotated_image && preview) preview.src = 'data:image/jpeg;base64,' + data.annotated_image;
          const title = data.plate_count ? `✓ 检测到 ${data.plate_count} 个车牌` : '○ 未检测到车牌';
          const subtitle = data.plates?.map(p => p.plate_number).filter(Boolean).join('、') || '等待下一帧';
          if (videoResult) {
            videoResult.innerHTML = `<div class="result-banner ${data.plate_count ? 'success' : 'danger'}"><div class="result-title">${title}</div><div class="result-subtitle">${subtitle}</div><div class="result-subtitle">runtime_api / yolo_lprnet_assets · 帧 ${data.frame ?? frameCount}</div></div>`;
          }
          if (plateTarget) {
            plateTarget.innerHTML = (data.plates || []).map(p => `<div class="plate-item"><span class="number">${this.formatPlateNumber(p.plate_number)}</span><span class="color ${this.plateColorClass(p.plate_color)}">${p.plate_color || '蓝牌'}</span><span class="history-meta" style="margin-left:.5rem">${((p.confidence || 0) * 100).toFixed(0)}%</span></div>`).join('') || '<p style="color:var(--text-muted)">未检测到车牌</p>';
          }
          if (progressFill) progressFill.style.width = Math.min(100, 5 + sentCount * 2) + '%';
          if (progressText) progressText.textContent = `实时识别中 · 已处理 ${frameCount} 帧`;
          (data.plates || []).forEach(p => {
            const plate = (p.plate_number || '').trim();
            const conf = Number(p.confidence || 0);
            if (!plate || conf < 0.65) return;
            const key = `${plate}|${p.plate_color || '蓝牌'}`;
            if (seenPlateKeys.has(key)) return;
            seenPlateKeys.add(key);
            seenVideoPlates.push({
              plate_number: plate,
              plate_color: p.plate_color || '蓝牌',
              confidence: conf,
              frame_index: data.frame ?? frameCount,
              source: 'yolo_lprnet',
            });
            console.log('[LPR-VIDEO] accumulate plate', plate, conf.toFixed(3), 'frame=', data.frame ?? frameCount);
          });
        }
      };
      this.lprVideoWs.onerror = (err) => {
        debug('websocket error', err);
      };
      await new Promise((resolve, reject) => {
        this.lprVideoWs.onopen = () => resolve();
        this.lprVideoWs.onerror = () => reject(new Error('实时识别 WebSocket 连接失败'));
      });
      debug('websocket ready, start timer');
      if (progressText) progressText.textContent = '识别连接已建立，正在播放并发送视频帧…';
      const ctx = canvas ? canvas.getContext('2d') : null;
      this.lprVideoTimer = setInterval(() => {
        if (!video) return;
        if (video.paused || video.ended) return;
        if (video.readyState < 2) return;
        if (this.lprVideoBusy || !this.lprVideoWs || this.lprVideoWs.readyState !== WebSocket.OPEN) return;
        if (!canvas || !ctx) return;
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
        this.lprVideoBusy = true;
        sentCount += 1;
        debug('send frame', sentCount, 'time=', video.currentTime, 'size=', dataUrl.length);
        this.lprVideoWs.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1] }));
      }, 250);
      video.onended = async () => {
        debug('video ended');
        if (progressText) progressText.textContent = '视频播放完成';
        if (progressFill) progressFill.style.width = '100%';
        if (this.lprVideoWs && this.lprVideoWs.readyState === WebSocket.OPEN) {
          this.lprVideoWs.send(JSON.stringify({ type: 'end' }));
          debug('sent end message');
        } else {
          debug('skip end message: websocket not open');
        }
        if (seenVideoPlates.length) {
          try {
            const headers = { 'Content-Type': 'application/json' };
            if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
            const resp = await fetch('/api/lpr/video-history', {
              method: 'POST',
              headers,
              body: JSON.stringify({
                plates: seenVideoPlates,
                source_path: file?.name || '',
                annotated_image: null,
              }),
            });
            const saved = await resp.json().catch(() => ({}));
            console.log('[LPR-FRONT] video-history', resp.status, saved);
            if (resp.ok && saved.saved) {
              if (progressText) progressText.textContent = '视频播放完成，历史记录已保存';
              this.loadLprHistory();
              this.loadDashboard();
            } else if (progressText) {
              progressText.textContent = saved.message || '视频播放完成，但历史未保存';
            }
          } catch (err) {
            console.warn('[LPR-FRONT] video-history failed', err);
          }
        }
        this.setLprLoading(false);
      };
    } catch (e) {
      debug('startVideoFileStream failed', e);
      if (progressText) progressText.textContent = '视频识别失败';
      if (videoResult) videoResult.innerHTML = `<div class="result-banner danger"><div class="result-title">视频识别失败</div><div class="result-subtitle">${e.message}</div></div>`;
      if (playbackTools) playbackTools.classList.add('hidden');
      alert(e.message);
      this.setLprLoading(false);
    }
  },


  async stopVideoStream() {
    if (this.lprVideoTimer) { clearInterval(this.lprVideoTimer); this.lprVideoTimer = null; }
    if (this.lprVideoWs) { this.lprVideoWs.close(); this.lprVideoWs = null; }
    if (this.lprRtspTimer) { clearInterval(this.lprRtspTimer); this.lprRtspTimer = null; }
    if (this.lprRtspWs) { this.lprRtspWs.close(); this.lprRtspWs = null; }
    if (this.lprRtspCapture) { clearInterval(this.lprRtspCapture); this.lprRtspCapture = null; }
    if (this.lprPreviewPoll) { clearInterval(this.lprPreviewPoll); this.lprPreviewPoll = null; }
    this.lprVideoBusy = false;
    this.lprRtspBusy = false;
    this.lprVideoMode = null;
    this.lprRtspMode = null;
    const sourceName = this.lprRtspSourceName || 'live1';
    this.lprRtspSourceName = null;
    this.setLprLoading(false);
    const fileVideo = document.getElementById('lpr-video-output');
    if (fileVideo) {
      fileVideo.pause();
      fileVideo.hidden = true;
      fileVideo.removeAttribute('src');
      fileVideo.load();
    }
    const progress = document.getElementById('lpr-video-progress');
    if (progress) progress.classList.add('hidden');
    const camVideo = document.getElementById('lpr-video');
    if (camVideo?.srcObject) {
      camVideo.srcObject.getTracks().forEach(t => t.stop());
      camVideo.srcObject = null;
    }
    if (camVideo) camVideo.hidden = true;
    const rtspStatus = document.getElementById('lpr-rtsp-status');
    const rtspProgress = document.getElementById('lpr-rtsp-progress');
    const rtspResult = document.getElementById('lpr-rtsp-result');
    if (rtspStatus) rtspStatus.textContent = '尚未启动 RTSP 识别';
    if (rtspProgress) rtspProgress.classList.add('hidden');
    if (rtspResult) rtspResult.innerHTML = '';
    const rtspDebug = document.getElementById('lpr-rtsp-debug');
    if (rtspDebug) rtspDebug.textContent = '预览未加载';
    const rtspImg = document.getElementById('lpr-rtsp-video');
    if (rtspImg) { rtspImg.removeAttribute('src'); rtspImg.src = ''; }
    try {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
      await fetch('/api/lpr/rtsp/stop', {
        method: 'POST',
        headers,
        body: JSON.stringify({ source_name: sourceName }),
      });
    } catch (e) {
      console.warn('[STOP-RTSP]', e);
    }
    this.stopStream();
  },

  stopStream() {
    if (this.streamInterval) { clearInterval(this.streamInterval); this.streamInterval = null; }
    if (this.wsStream) { this.wsStream.close(); this.wsStream = null; }
    if (this.streamModule) {
      const video = document.getElementById(this.streamModule + '-video');
      if (video.srcObject) { video.srcObject.getTracks().forEach(t => t.stop()); video.srcObject = null; }
      video.hidden = true;
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

  captureAndSendLprFrame(video, canvas) {
    if (this.lprRtspMode === 'rtsp') return;
    if (!video || !canvas || video.readyState < 2 || this.lprVideoBusy || !this.lprVideoWs || this.lprVideoWs.readyState !== WebSocket.OPEN) return;
    const ctx = canvas.getContext('2d');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
    this.lprVideoBusy = true;
    this.lprVideoWs.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1] }));
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
