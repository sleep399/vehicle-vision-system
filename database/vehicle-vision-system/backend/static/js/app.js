const App = {
  token: localStorage.getItem('token') || '',
  streamModule: null,
  streamInterval: null,
  streamBusy: false,
  streamTimeout: null,
  wsAlerts: null,
  wsStream: null,
  alertSse: null,
  logSse: null,
  lprVideoWs: null,
  lprVideoBusy: false,
  lprVideoTimer: null,
  lprVideoMode: null,
  uploadedRecognitionResults: [],
  policeHistoryLastSaved: {},
  policeHistorySaveGapMs: 3000,
  ownerCurrentControl: 'volume_up',
  ownerLastGestureUntil: 0,
  ownerLastGestureHtml: '',
  ownerStandbyDismissed: false,
  ownerStandbyLockedUntil: 0,
  focusedAlertId: null,
  focusedAlertTitle: '',

  init() {
    this.bindTabs();
    this.bindNav();
    this.bindFileInputs();
    this.bindLprDragDrop();
    this.bindPoliceImageViewer();
    if (this.initSelectChevrons) this.initSelectChevrons();
    if (this.initAssistant) this.initAssistant();
    if (this.token) this.showMain();
    else document.getElementById('login-page').classList.add('active');
  },

  BACKEND_PORT: 8001,

  backendOrigin() {
    const host = location.hostname || 'localhost';
    const proto = location.protocol === 'https:' ? 'https' : 'http';
    return `${proto}://${host}:${this.BACKEND_PORT}`;
  },

  apiUrl(path) {
    if (!path || /^https?:\/\//i.test(path)) return path;
    return `${this.backendOrigin()}${path.startsWith('/') ? path : `/${path}`}`;
  },

  wsBase() {
    const host = location.hostname || 'localhost';
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${host}:${this.BACKEND_PORT}`;
  },

  headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this.token) h['Authorization'] = `Bearer ${this.token}`;
    return h;
  },

  async api(path, opts = {}) {
    const res = await fetch(this.apiUrl(path), { ...opts, headers: { ...this.headers(), ...opts.headers } });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || '请求失败');
    }
    return res.json();
  },

  shouldSavePoliceHistory(data) {
    const gesture = data?.gesture || 'no_gesture';
    const now = Date.now();
    const last = this.policeHistoryLastSaved[gesture] || 0;
    const previousGesture = this.policeHistoryLastSaved._lastGesture;
    if (gesture !== previousGesture || now - last >= this.policeHistorySaveGapMs) {
      this.policeHistoryLastSaved[gesture] = now;
      this.policeHistoryLastSaved._lastGesture = gesture;
      return true;
    }
    return false;
  },

  async savePoliceHistoryRecord(data, sourceType = 'stream') {
    if (!data || !this.shouldSavePoliceHistory(data)) return;
    try {
      await this.api('/api/police-gesture/history', {
        method: 'POST',
        body: JSON.stringify({
          source_type: sourceType,
          gesture_id: data.gesture_id,
          gesture: data.gesture || 'no_gesture',
          gesture_cn: data.gesture_cn || '无手势',
          confidence: data.confidence ?? 0,
          keypoints: data.keypoints || [],
          annotated_image: data.annotated_image || null,
        }),
      });
      this.loadPoliceHistory();
    } catch (e) {
      console.warn('[POLICE] history save failed', e);
    }
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
    document.getElementById('lpr-file').onchange = (e) => this.handleLprInput(e.target.files);
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
      const files = e.dataTransfer?.files;
      if (files?.length) this.handleLprInput(files);
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
          ? '视频识别：RPNet 已就绪'
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
    const previousView = this.currentView;
    this.currentView = view;
    if (previousView === 'logs' && view !== 'logs' && this.disconnectLogStream) this.disconnectLogStream();
    if (view === 'dashboard') this.loadDashboard();
    if (view === 'lpr') { this.loadLprHistory(); this.loadLprModelStatus(); }
    if (view === 'police') { this.loadPolicePoseBackend(); this.loadPoliceGestures(); this.loadPoliceHistory(); this.ensureCameraSelector('police'); }
    if (view === 'owner') { this.loadOwnerGestures(); this.loadVehicleState(); this.refreshCameraDevices('owner'); }
    if (view === 'alerts') {
      this.connectAlertWs();
      if (this.connectSSE) this.connectSSE();
      this.loadAlerts();
      this.loadAlertTypes();
      if (this.loadScenarioFusion) this.loadScenarioFusion();
      if (this.loadAlertAnalytics) this.loadAlertAnalytics();
      if (this.loadAgentActivity) this.loadAgentActivity();
      if (this.loadAlertNotifications) this.loadAlertNotifications();
      if (this.loadAlertConfig) this.loadAlertConfig();
    }
    if (view === 'logs') {
      if (this.resetLogFilters) this.resetLogFilters(false);
      if (this.syncLogDatetimeState) this.syncLogDatetimeState();
      this.loadLogs();
      if (this.connectLogStream) this.connectLogStream();
    }
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

  async register() {
    const username = document.getElementById('register-user').value.trim();
    const password = document.getElementById('register-pass').value;
    const email = document.getElementById('register-email').value.trim() || null;
    const phone = document.getElementById('register-phone').value.trim() || null;
    if (!username || !password) {
      alert('请输入用户名和密码');
      return;
    }
    try {
      const data = await this.api('/api/auth/register', {
        method: 'POST',
        body: JSON.stringify({ username, password, email, phone }),
      });
      this.token = data.access_token;
      localStorage.setItem('token', this.token);
      this.showMain();
    } catch (e) { alert(e.message); }
  },

  escHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[ch]);
  },

  monitorLevelLabel(level) {
    return ({ info: '提示', warning: '警告', critical: '严重', INFO: '信息', WARN: '警告', WARNING: '警告', ERROR: '错误', CRITICAL: '严重' })[level] || level || '信息';
  },

  monitorCategoryLabel(category) {
    return ({ lpr: '车牌识别', police_gesture: '交警手势', owner_gesture: '车主手势', alert: '告警', user: '用户操作', system: '系统运行', agent: '智能体决策' })[category] || category || '系统运行';
  },

  bindPoliceImageViewer() {
    ['police-stream-preview', 'police-preview'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', event => {
        const src = event.currentTarget?.src;
        if (src) this.openPoliceImageViewer(src);
      });
    });
    document.getElementById('police-image-viewer')?.addEventListener('click', event => {
      if (event.target === event.currentTarget) this.closePoliceImageViewer();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') this.closePoliceImageViewer();
    });
  },

  openPoliceImageViewer(src) {
    const viewer = document.getElementById('police-image-viewer');
    const image = document.getElementById('police-image-viewer-img');
    if (!viewer || !image || !src) return;
    image.src = src;
    viewer.hidden = false;
    document.body.style.overflow = 'hidden';
  },

  closePoliceImageViewer() {
    const viewer = document.getElementById('police-image-viewer');
    const image = document.getElementById('police-image-viewer-img');
    if (!viewer || viewer.hidden) return;
    viewer.hidden = true;
    if (image) image.removeAttribute('src');
    document.body.style.overflow = '';
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
      qrBox.innerHTML = `<img src="${session.qrcode_url}" alt="微信扫码登录二维码"><small>请用手机扫描后确认（演示模式）</small>`;
      const poll = setInterval(async () => {
        const res = await fetch(this.apiUrl(session.poll_url));
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
    if (this.connectSSE) this.connectSSE();
    if (this.refreshAgentStats) this.refreshAgentStats();
    if (this.startAgentMonitorLoop) this.startAgentMonitorLoop();
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
    if (this.disconnectSSE) this.disconnectSSE();
    if (this.stopAgentMonitorLoop) this.stopAgentMonitorLoop();
    location.reload();
  },

  connectAlertWs() {
    if (this.wsAlerts) return;
    this.wsAlerts = new WebSocket(`${this.wsBase()}/ws/alerts`);
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

  async handleLprInput(input) {
    const files = Array.from(input?.target?.files || input || []).filter(Boolean);
    if (!files.length) return;
    const videoFiles = files.filter(file => this.isVideoFile(file));
    const imageFiles = files.filter(file => !this.isVideoFile(file));
    if (videoFiles.length > 0 && files.length > 1) {
      alert('图片识别支持多张同时识别，视频请单独选择一个文件。');
      return;
    }
    if (videoFiles.length === 1) {
      this.clearLprDisplay();
      this.setLprLoading(true, { forceHide: true });
      await this.startVideoFileStream(videoFiles[0]);
      return;
    }
    if (!imageFiles.length) return;
    this.clearLprDisplay();
    const resultBox = document.getElementById('lpr-results');
    if (resultBox) resultBox.innerHTML = '<div class="result-banner"><div class="result-title">正在识别多张图片，请稍候…</div></div>';
    const batchResults = [];
    for (const file of imageFiles) {
      const isCcpd = this.isCcpdFilename(file.name);
      const data = await this.uploadFile('lpr', file, {
        forceModel: !isCcpd, ccpd: isCcpd, skipClear: true, skipAlert: true, returnData: true,
      });
      if (data) batchResults.push({ file, data });
    }
    if (batchResults.length) this.renderLprBatchResults(batchResults, 0);
  },

  async uploadFile(module, file, options = {}) {
    if (!file) return;
    if (module === 'lpr' && !options.skipClear) this.clearLprDisplay();
    const isVideo = this.isVideoFile(file);
    const endpoints = {
      lpr: '/api/lpr/recognize',
      owner: isVideo ? '/api/owner-gesture/recognize-video' : '/api/owner-gesture/recognize',
    };
    const previewMap = { lpr: 'lpr-preview', police: 'police-preview', owner: 'owner-preview' };
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const preview = document.getElementById(previewMap[module]);
    const resultBox = document.getElementById(resultMap[module]);
    if (module === 'police') {
      if (!isVideo) {
        if (resultBox) resultBox.innerHTML = '<div class="result-banner error"><div class="result-title">交警手势仅支持视频或连续视频流</div></div>';
        return;
      }
      this.showPoliceUploadPreview(file, isVideo);
      if (isVideo) {
        if (resultBox) resultBox.innerHTML = '播放视频后开始实时识别...';
        this.startUploadedPoliceVideo();
        return;
      }
    } else if (preview && file.type.startsWith('image/')) {
      preview.src = URL.createObjectURL(file);
    }
    if (resultBox) resultBox.innerHTML = isVideo && module === 'owner'
      ? '<div class="result-banner"><div class="result-title">正在解析视频并逐帧识别，请稍候…</div></div>'
      : '<div class="result-banner"><div class="result-title">正在识别，请稍候…</div></div>';
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
        const res = await fetch(this.apiUrl(url), { method: 'POST', body: form, headers });
        data = await res.json();
        if (!res.ok) throw new Error(data.detail || '识别失败');
      }
      if (module === 'owner' && isVideo) this.renderOwnerVideoPayload(data);
      else this.renderResult(module, data);
      if (module === 'owner' && !isVideo && data.action) this.loadVehicleState();
      if (options.returnData) return data;
    } catch (e) {
      if (resultBox) resultBox.innerHTML = `<div class="result-banner danger"><div class="result-title">识别失败</div><div class="result-subtitle">${e.message}</div></div>`;
      if (!options.skipAlert) alert(e.message);
    } finally {
      if (module === 'lpr' && !options.skipClear) this.setLprLoading(false);
    }
  },

  renderOwnerVideoPayload(payload) {
    const result = payload.best_result || payload.preview_result;
    if (result) this.renderResult('owner', result);
    const target = document.getElementById('owner-result');
    if (target) {
      const hits = (payload.results || []).slice(0, 8).map(item =>
        `<div class="video-summary-item">帧 ${item.frame}: ${item.gesture_cn} (${Math.round((item.confidence || 0) * 100)}%)</div>`
      ).join('');
      target.insertAdjacentHTML('beforeend', `
        <div class="video-summary">
          <div class="video-summary-title">视频识别摘要</div>
          <div class="video-summary-meta">总帧 ${payload.frame_count || 0} · 采样 ${payload.sampled_frames || 0} · 命中 ${payload.recognized_frames || 0}</div>
          ${hits || '<div class="video-summary-item">未命中有效手势，但已完成视频分析。</div>'}
        </div>`);
    }
    const state = payload.vehicle_state || payload.final_vehicle_state || result?.vehicle_state;
    if (state) this.applyVehicleState(state);
    if (result?.action === 'go_home' || (state?.current_page === 'standby' && !state?.is_awake)) {
      this.forceStandby(1500);
    }
  },

  ownerActionLabel(action) {
    return {
      wake: '唤醒系统',
      confirm: '确认当前功能',
      volume_adjust: '调节当前选中项',
      prev_page: '选择上一个功能',
      next_page: '选择下一个功能',
      answer_call: '接听电话',
      hang_up: '挂断电话',
      go_home: '返回待机主页',
    }[action] || action || '-';
  },

  showPoliceUploadPreview(file, isVideo) {
    const imagePreview = document.getElementById('police-preview');
    const videoPreview = document.getElementById('police-upload-preview');
    const cameraVideo = document.getElementById('police-video');
    const streamPreview = document.getElementById('police-stream-preview');
    const canvas = document.getElementById('police-canvas');
    const controls = document.getElementById('police-upload-controls');
    const playButton = document.getElementById('police-upload-play');
    const url = URL.createObjectURL(file);
    if (isVideo) {
      if (cameraVideo) cameraVideo.hidden = true;
      if (canvas) canvas.hidden = true;
      if (streamPreview) {
        streamPreview.removeAttribute('src');
        streamPreview.hidden = true;
      }
      if (videoPreview) {
        videoPreview.pause();
        videoPreview.src = url;
        videoPreview.preload = 'auto';
        videoPreview.playsInline = true;
        videoPreview.controls = true;
        videoPreview.hidden = false;
        videoPreview.load();
      }
      if (controls) controls.hidden = false;
      if (playButton) playButton.textContent = '播放视频';
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
    if (streamPreview) {
      streamPreview.removeAttribute('src');
      streamPreview.hidden = true;
    }
    if (controls) controls.hidden = true;
    if (imagePreview) {
      imagePreview.src = url;
      imagePreview.hidden = false;
    }
  },

  showAnnotatedPreview(module, base64Image) {
    const imagePreview = document.getElementById(module + '-preview');
    if (!imagePreview || !base64Image) return;
    imagePreview.src = 'data:image/jpeg;base64,' + base64Image;
    imagePreview.hidden = false;
  },

  showPoliceRecognitionFrame(base64Image) {
    const streamPreview = document.getElementById('police-stream-preview');
    const imagePreview = document.getElementById('police-preview');
    if (!streamPreview || !base64Image) return;
    streamPreview.src = 'data:image/jpeg;base64,' + base64Image;
    streamPreview.hidden = false;
    if (imagePreview) {
      imagePreview.removeAttribute('src');
      imagePreview.hidden = true;
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
    this.wsStream = new WebSocket(`${this.wsBase()}/ws/stream/police`);
    const sampleFps = 15;
    const sampleMs = 1000 / sampleFps;
    let processedFrames = 0;
    let lastSentAt = 0;
    let lastResultAt = -1;
    let waitingForFirstPlay = true;

    const renderSynchronizedResult = (row) => {
      if (!resultBox || !row) return;
      if (row.annotated_image) this.showPoliceRecognitionFrame(row.annotated_image);
      const now = Number.isFinite(video.currentTime) ? video.currentTime : row.time_sec;
      const lag = Math.max(0, now - row.time_sec);
      resultBox.innerHTML = `${row.gesture_cn}<br><small>置信度 ${(row.confidence * 100).toFixed(0)}%</small><br><small>video ${now.toFixed(1)}s / label ${row.time_sec.toFixed(1)}s / lag ${lag.toFixed(1)}s</small>`;
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
        const row = { ...msg.data, time_sec: Number(msg.time_sec ?? video.currentTime ?? 0) };
        lastResultAt = row.time_sec;
        this.uploadedRecognitionResults.push(row);
        renderSynchronizedResult(row);
        this.savePoliceHistoryRecord(row, 'video');
      }
      if (msg.type === 'frame_error' && resultBox) {
        resultBox.innerHTML = `视频帧识别失败：${msg.message}`;
      }
    };

    this.wsStream.onerror = () => {
      if (resultBox) resultBox.innerHTML = '实时视频识别连接失败';
    };
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
          ? 'RPNet 未加载，请将 fh02.pth 放到 backend/app/models/'
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
      this.showPoliceRecognitionFrame(data.annotated_image);
      document.getElementById('police-result').innerHTML = `${data.gesture_cn}<br><small>置信度 ${(data.confidence*100).toFixed(0)}%</small>`;
      this.loadPoliceHistory();
    } else if (module === 'owner') {
      if (this.isOwnerStandbyLocked() && !this.isWakeResult(data)) {
        this.showStandby();
        return;
      }
      const preview = document.getElementById('owner-preview');
      if (preview && data.annotated_image) preview.src = 'data:image/jpeg;base64,' + data.annotated_image;
      const resultBox = document.getElementById('owner-result');
      if (resultBox) {
        const now = Date.now();
        if (data.gesture && data.gesture !== 'no_gesture') {
          const confidence = Math.round((data.confidence || 0) * 100);
          const color = data.confidence >= 0.85 ? '#3ddc84' : (data.confidence >= 0.6 ? '#f5a623' : '#f5533d');
          const shouldHold = data.gesture !== 'palm_open';
          this.ownerLastGestureUntil = shouldHold ? now + 1200 : now;
          this.ownerLastGestureHtml = `
            <div class="gesture-name">${data.gesture_cn}</div>
            <div class="conf-bar-wrap"><div class="conf-bar" style="width:${confidence}%;background:${color}"></div></div>
            <div class="conf-text">置信度 ${confidence}% · ${shouldHold ? '短暂停留 1.2 秒' : '实时显示'}</div>
            ${data.action ? `<div class="action-tag">→ ${this.ownerActionLabel(data.action)}</div>` : ''}`;
          resultBox.innerHTML = this.ownerLastGestureHtml;
        } else if (now < this.ownerLastGestureUntil && this.ownerLastGestureHtml) {
          resultBox.innerHTML = this.ownerLastGestureHtml;
        } else {
          this.ownerLastGestureHtml = '';
          resultBox.innerHTML = '<span style="color:var(--text-muted)">未识别到手势，请将手部完整放入画面并保持光线充足</span>';
        }
      }
      if (data.confirmation_resolved) this.hideGestureConfirm();
      if (data.needs_confirmation) this.showGestureConfirm(data.confirm_prompt);
      if (data.action === 'go_home') this.forceStandby(1500);
      // Realtime recognition responses also carry the last persisted state. A
      // no-action frame must not overwrite a manual slider/button edit that is
      // being saved at the same time.
      const shouldApplyVehicleState = !opts.realtime || Boolean(data.action) || Boolean(data.confirmation_resolved);
      if (data.vehicle_state && shouldApplyVehicleState) this.applyVehicleState(data.vehicle_state);
      else if (data.action && !data.vehicle_state) this.loadVehicleState();
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
      const res = await fetch(this.apiUrl(`/api/lpr/recognize-ccpd?relative=${encodeURIComponent(relative)}`), {
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

  async loadPolicePoseBackend() {
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
      const data = await this.api('/api/police-gesture/history?limit=20');
      document.getElementById('police-history').innerHTML = data.map(r => {
        const source = { image: '图片', video: '视频', camera: '摄像头', stream: '视频流' }[r.source_type] || r.source_type || '识别';
        return `<div class="history-item">
          <div>
            <span>${r.gesture_cn}</span>
            <div class="history-meta">#${r.id} · ${source} · ${new Date(r.created_at).toLocaleString()}</div>
          </div>
          <span class="history-meta">${(r.confidence*100).toFixed(0)}%</span>
        </div>`;
      }).join('') || '<p style="color:var(--text-muted)">暂无记录</p>';
    } catch (e) {}
  },

  async loadOwnerGestures() {
    try {
      const data = await this.api('/api/owner-gesture/gestures');
      document.getElementById('owner-gestures').innerHTML = data.map(g =>
        `<span class="gesture-tag">${g.cn} → ${this.ownerActionLabel(g.action)}</span>`
      ).join('');
    } catch (e) {}
  },

  showGestureConfirm(prompt) {
    let box = document.getElementById('gesture-confirm');
    if (!box) {
      box = document.createElement('div');
      box.id = 'gesture-confirm';
      box.className = 'gesture-confirm';
      document.body.appendChild(box);
    }
    box.innerHTML = `<div class="gc-card"><div class="gc-msg">${prompt || '检测到低置信度手势，是否确认执行？'}</div><div class="gc-btns"><button class="btn primary" id="gc-yes">确认</button><button class="btn" id="gc-no">取消</button></div></div>`;
    box.style.display = 'flex';
    document.getElementById('gc-yes').onclick = () => this.respondGestureConfirm(true);
    document.getElementById('gc-no').onclick = () => this.respondGestureConfirm(false);
  },

  hideGestureConfirm() {
    const box = document.getElementById('gesture-confirm');
    if (box) box.style.display = 'none';
  },

  async respondGestureConfirm(accept) {
    if (this.wsStream?.readyState === WebSocket.OPEN && this.streamModule === 'owner') {
      this.wsStream.send(JSON.stringify({ type: 'confirm', accept }));
    } else {
      const result = await this.api(`/api/owner-gesture/confirm?accept=${accept}`, { method: 'POST' });
      if (result.vehicle_state) this.applyVehicleState(result.vehicle_state);
    }
    this.hideGestureConfirm();
  },

  applyVehicleState(s) {
    document.getElementById('v-awake').textContent = s.is_awake ? '已唤醒' : '休眠';
    this.ownerCurrentControl = s.current_page || 'volume_up';
    const names = { volume_up: '音量 +', volume_down: '音量 -', temp_up: '温度 +', temp_down: '温度 -', standby: '待机主页' };
    document.getElementById('v-page').textContent = names[s.current_page] || s.current_page;
    document.getElementById('v-volume').value = s.volume;
    document.getElementById('v-volume-val').textContent = s.volume;
    document.getElementById('v-temp').value = s.temperature;
    document.getElementById('v-temp-val').textContent = s.temperature;
    document.getElementById('v-phone').textContent = s.phone_status === 'in_call' ? '通话中' : '空闲';
    this.updateOwnerFunctionHighlight(s.current_page);
    if (s.current_page === 'standby' && !s.is_awake) {
      if (this.isOwnerStandbyLocked() || !this.ownerStandbyDismissed) this.showStandby();
      else this.hideStandby();
    } else {
      this.ownerStandbyLockedUntil = 0;
      this.ownerStandbyDismissed = false;
      this.hideStandby();
    }
  },

  updateOwnerFunctionHighlight(current) {
    const selected = current && current !== 'standby' ? current : 'volume_up';
    document.querySelectorAll('#owner-function-selector .function-card').forEach(card => {
      card.classList.toggle('active', card.dataset.control === selected);
    });
  },

  isOwnerStandbyLocked() {
    return Date.now() < (this.ownerStandbyLockedUntil || 0);
  },

  isWakeResult(data) {
    if (!data) return false;
    if (data.action === 'wake') return true;
    if (data.vehicle_state?.is_awake) return true;
    return data.gesture === 'palm_open' && data.confidence >= 0.8;
  },

  forceStandby(lockMs = 0) {
    this.ownerStandbyDismissed = false;
    this.ownerStandbyLockedUntil = Math.max(
      this.ownerStandbyLockedUntil || 0,
      Date.now() + Math.max(0, lockMs),
    );
    this.showStandby();
  },

  showStandby() {
    const page = document.getElementById('standby-page');
    if (!page) return;
    const update = () => {
      const now = new Date();
      document.getElementById('standby-clock').textContent = now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
      document.getElementById('standby-date').textContent = now.toLocaleDateString('zh-CN', { weekday: 'long', month: 'long', day: 'numeric' });
    };
    update();
    page.hidden = false;
    page.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    this._standbyTimer = this._standbyTimer || setInterval(update, 1000);
  },

  hideStandby() {
    const page = document.getElementById('standby-page');
    if (page) {
      page.hidden = true;
      page.setAttribute('aria-hidden', 'true');
    }
    document.body.style.overflow = '';
  },

  exitStandby() {
    this.ownerStandbyDismissed = true;
    this.ownerStandbyLockedUntil = 0;
    this.hideStandby();
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('.nav-item[data-view="owner"]')?.classList.add('active');
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById('view-owner')?.classList.add('active');
  },

  async loadVehicleState() {
    try {
      const s = await this.api('/api/owner-gesture/vehicle-state');
      this.applyVehicleState(s);
    } catch (e) {}
  },

  async updateVehicle() {
    const data = {
      volume: +document.getElementById('v-volume').value,
      temperature: +document.getElementById('v-temp').value,
      phone_status: document.getElementById('v-phone').textContent === '通话中' ? 'in_call' : 'idle',
      current_page: this.ownerCurrentControl || 'volume_up',
      is_awake: document.getElementById('v-awake').textContent === '已唤醒' ? 1 : 0,
    };
    document.getElementById('v-volume-val').textContent = data.volume;
    document.getElementById('v-temp-val').textContent = data.temperature;
    try {
      const saved = await this.api('/api/owner-gesture/vehicle-state', { method: 'PUT', body: JSON.stringify(data) });
      this.applyVehicleState(saved);
    } catch (e) {}
  },

  setPhone(status) {
    document.getElementById('v-phone').textContent = status === 'in_call' ? '通话中' : '空闲';
    this.updateVehicle();
  },

  async ensureCameraSelector(module) {
    if (document.getElementById(`${module}-camera-device`)) {
      await this.refreshCameraDevices(module);
      return;
    }
    if (module !== 'police') return;
    const streamUrlRow = document.getElementById('police-stream-url')?.closest('.stream-url-row');
    if (!streamUrlRow) return;
    const row = document.createElement('div');
    row.className = 'camera-device-row';
    row.id = 'police-camera-row';
    row.innerHTML = `
      <select id="police-camera-device">
        <option value="">默认摄像头</option>
      </select>
      <button class="btn" type="button" onclick="App.refreshCameraDevices('police')">刷新摄像头</button>
      <span id="police-camera-status" class="camera-status">尚未检测摄像头</span>
    `;
    streamUrlRow.parentNode.insertBefore(row, streamUrlRow);
    await this.refreshCameraDevices(module);
  },

  async refreshCameraDevices(module) {
    const select = document.getElementById(`${module}-camera-device`);
    const status = document.getElementById(`${module}-camera-status`);
    if (!select || !navigator.mediaDevices?.enumerateDevices) {
      if (status) status.textContent = '当前浏览器不支持摄像头设备枚举';
      return [];
    }
    const current = select.value;
    const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
    const cameras = devices.filter(d => d.kind === 'videoinput');
    const automaticLabel = module === 'owner' ? '自动选择（优先笔记本前置摄像头）' : '默认摄像头';
    select.innerHTML = `<option value="">${automaticLabel}</option>` + cameras.map((d, i) =>
      `<option value="${d.deviceId}">${d.label || `摄像头 ${i + 1}`}</option>`
    ).join('');
    if (current && cameras.some(d => d.deviceId === current)) select.value = current;
    if (status) status.textContent = cameras.length ? `检测到 ${cameras.length} 个摄像头` : '未检测到可用摄像头';
    return cameras;
  },

  cameraDevicePriority(device) {
    const label = (device?.label || '').toLowerCase();
    if (/integrated|built[- ]?in|front|user|facetime|内置|前置/.test(label)) return 0;
    if (/virtual|obs|manycam|snap camera|droidcam|iriun|虚拟/.test(label)) return 2;
    return 1;
  },

  async ensurePoliceCameraSelector() {
    return this.ensureCameraSelector('police');
  },

  async refreshPoliceCameraDevices() {
    return this.refreshCameraDevices('police');
  },

  cameraErrorMessage(error) {
    const name = error?.name || '';
    const detail = error?.message || String(error || '');
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      return '浏览器没有摄像头权限，请在地址栏左侧允许摄像头权限后刷新页面。';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return '没有找到可用摄像头，请确认摄像头已连接并被系统识别。';
    }
    if (name === 'NotReadableError' || /Could not start video source/i.test(detail)) {
      return '摄像头无法启动，通常是被微信、腾讯会议、系统相机、浏览器其它标签页占用了。请关闭占用摄像头的软件后重试。';
    }
    if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') {
      return '当前摄像头不支持请求的分辨率或帧率，已尝试降级仍失败。';
    }
    if (!window.isSecureContext) {
      return '当前页面不是安全上下文。请使用 localhost、127.0.0.1 或 https 访问。';
    }
    return `无法访问摄像头：${detail}`;
  },

  async openCameraStream(module) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('当前浏览器不支持 getUserMedia 摄像头接口');
    }
    await this.ensureCameraSelector(module);
    const select = document.getElementById(`${module}-camera-device`);
    const status = document.getElementById(`${module}-camera-status`);
    const selectedDevice = select?.value || '';
    const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
    const cameras = devices
      .filter(device => device.kind === 'videoinput' && device.deviceId)
      .sort((left, right) => this.cameraDevicePriority(left) - this.cameraDevicePriority(right));
    const baseVideo = {
      width: { ideal: 640 },
      height: { ideal: 480 },
      frameRate: { ideal: 15, max: 15 },
    };
    const attempts = [];
    const orderedDeviceIds = [
      ...(selectedDevice ? [selectedDevice] : []),
      ...cameras.map(device => device.deviceId).filter(deviceId => deviceId !== selectedDevice),
    ];
    for (const deviceId of orderedDeviceIds) {
      // First avoid all resolution/FPS constraints: many laptop cameras reject
      // constrained startup even though they can provide frames normally.
      attempts.push({ video: { deviceId: { exact: deviceId } }, audio: false });
      attempts.push({ video: { ...baseVideo, deviceId: { exact: deviceId } }, audio: false });
    }
    attempts.push({ video: { ...baseVideo, facingMode: { ideal: 'user' } }, audio: false });
    attempts.push({ video: baseVideo, audio: false });
    attempts.push({ video: true, audio: false });

    let lastError = null;
    for (const constraints of attempts) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        const activeTrack = stream.getVideoTracks()[0];
        const activeDeviceId = activeTrack?.getSettings?.().deviceId || '';
        const activeCamera = cameras.find(device => device.deviceId === activeDeviceId);
        if (select && activeDeviceId && [...select.options].some(option => option.value === activeDeviceId)) {
          select.value = activeDeviceId;
        }
        if (status) status.textContent = `已连接：${activeTrack?.label || activeCamera?.label || '摄像头'}`;
        return stream;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error('摄像头启动失败');
  },

  async openPoliceCameraStream() {
    return this.openCameraStream('police');
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
      const resultMap = { police: 'police-result', owner: 'owner-result' };
      const resultBox = document.getElementById(resultMap[module]);
      if (resultBox) resultBox.innerHTML = '正在请求摄像头权限…';
      const streamPreview = module === 'police' ? document.getElementById('police-stream-preview') : null;
      if (streamPreview) {
        streamPreview.hidden = true;
        streamPreview.removeAttribute('src');
      }
      const stream = await this.openCameraStream(module);
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      video.hidden = false;
      canvas.hidden = true;
      await video.play();
      await this.refreshCameraDevices(module);
      if (resultBox) resultBox.innerHTML = '摄像头已打开，正在连接识别服务…';
      const ctx = canvas.getContext('2d');
      const statusEl = document.getElementById('lpr-video-model-status');
      if (statusEl) statusEl.textContent = '摄像头已打开，等待识别结果…';

      const wsUrl = module === 'owner'
        ? `${this.wsBase()}/api/owner-gesture/ws-stream?token=${encodeURIComponent(this.token || '')}`
        : `${this.wsBase()}/ws/stream/${module}`;
      this.wsStream = new WebSocket(wsUrl);
      this.wsStream.onopen = () => {
        if (resultBox) resultBox.innerHTML = '摄像头和识别服务已连接，等待手势…';
        if (module === 'owner') this.wsStream.send(JSON.stringify({ type: 'ping' }));
      };
      this.wsStream.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        this.streamBusy = false;
        if (msg.type === 'result') {
          if (module === 'owner' && msg.data?.action === 'go_home') this.forceStandby(1500);
          this.renderResult(module, msg.data, { realtime: module === 'owner' });
          if (module === 'police') this.savePoliceHistoryRecord(msg.data, 'camera');
        }
        if (msg.type === 'confirmed') {
          if (msg.data?.vehicle_state) this.applyVehicleState(msg.data.vehicle_state);
          this.hideGestureConfirm();
        }
        if (msg.type === 'frame_error' || msg.type === 'error') {
          const resultMap = { police: 'police-result', owner: 'owner-result' };
          const resultBox = document.getElementById(resultMap[module]);
          if (resultBox) resultBox.innerHTML = `识别失败：${msg.message}`;
        }
      };
      this.wsStream.onerror = () => {
        this.streamBusy = false;
        if (resultBox) resultBox.innerHTML = '摄像头已打开，但识别服务连接失败，请检查后端服务。';
      };
      this.wsStream.onclose = () => { this.streamBusy = false; };

      const frameIntervalMs = module === 'police' ? 80 : 200;
      this.streamInterval = setInterval(() => {
        const canSend = module === 'owner' || !this.streamBusy;
        if (video.readyState >= 2 && this.wsStream?.readyState === WebSocket.OPEN && canSend) {
          if (module !== 'owner') this.streamBusy = true;
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
          ctx.drawImage(video, 0, 0);
          const dataUrl = canvas.toDataURL('image/jpeg', module === 'owner' ? 0.6 : 0.7);
          this.wsStream.send(JSON.stringify({ type: 'frame', data: dataUrl.split(',')[1] }));
        }
      }, frameIntervalMs);
    } catch (e) {
      this.stopStream();
      const message = this.cameraErrorMessage(e);
      const resultMap = { police: 'police-result', owner: 'owner-result' };
      const resultBox = document.getElementById(resultMap[module]);
      if (resultBox) resultBox.innerHTML = message;
      alert(message);
    }
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
    const streamPreview = module === 'police' ? document.getElementById('police-stream-preview') : null;
    if (streamPreview) {
      const video = document.getElementById('police-video');
      const canvas = document.getElementById('police-canvas');
      if (video) video.hidden = true;
      if (canvas) canvas.hidden = true;
      streamPreview.hidden = false;
      streamPreview.removeAttribute('src');
    }

    this.wsStream = new WebSocket(`${this.wsBase()}/ws/stream-url/${module}`);
    this.wsStream.onopen = () => {
      this.wsStream.send(JSON.stringify({ type: 'start', url, interval: 1, target_fps: 15 }));
    };
    this.wsStream.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'status' && resultBox) resultBox.innerHTML = '视频流已连接，正在识别...';
      if (msg.type === 'result') {
        if (streamPreview && msg.data?.annotated_image) {
          streamPreview.src = 'data:image/jpeg;base64,' + msg.data.annotated_image;
          streamPreview.hidden = false;
        }
        this.renderResult(module, msg.data, { realtime: module === 'owner' });
        if (module === 'police') this.savePoliceHistoryRecord(msg.data, 'stream');
      }
      if (msg.type === 'error') {
        if (resultBox) resultBox.innerHTML = `视频流错误：${msg.message}`;
        else alert(msg.message);
      }
    };
    this.wsStream.onerror = () => {
      if (resultBox) resultBox.innerHTML = '视频流连接失败';
    };
  },

  renderLprBatchResults(batchResults, selectedIndex = 0) {
    this.uploadedRecognitionResults = batchResults.map((item, index) => ({ index, file: item.file, data: item.data }));
    this._batchSelectedIndex = Math.max(0, Math.min(selectedIndex, batchResults.length - 1));
    const nav = document.getElementById('lpr-batch-nav');
    if (nav) {
      nav.innerHTML = batchResults.map((_, index) => `<button class="batch-chip ${index === this._batchSelectedIndex ? 'active' : ''}" data-idx="${index}">第 ${index + 1} 张</button>`).join('');
      nav.querySelectorAll('button[data-idx]').forEach(btn => {
        btn.onclick = () => this.renderLprBatchResult(Number(btn.dataset.idx || 0));
      });
      nav.hidden = batchResults.length <= 1;
    }
    this.renderLprBatchResult(this._batchSelectedIndex);
  },

  renderLprBatchResult(index) {
    const item = this.uploadedRecognitionResults[index];
    if (!item) return;
    this._batchSelectedIndex = index;
    const data = item.data || {};
    const nav = document.getElementById('lpr-batch-nav');
    nav?.querySelectorAll('button[data-idx]').forEach(btn => {
      btn.classList.toggle('active', Number(btn.dataset.idx || 0) === index);
    });
    const preview = document.getElementById('lpr-preview');
    if (preview && data.annotated_image) preview.src = `data:image/jpeg;base64,${data.annotated_image}`;
    const resultBox = document.getElementById('lpr-image-result');
    const plateSummary = (data.plates || []).map(p => `${this.formatPlateNumber(p.plate_number)}（${p.plate_color || '蓝牌'}）`).join('、');
    if (resultBox) resultBox.innerHTML = `<div class="result-banner ${data.success ? 'success' : 'danger'}"><div class="result-title">第 ${index + 1} 张 · ${data.success ? '识别成功' : '未识别到有效车牌'}</div><div class="result-subtitle">${plateSummary || '无结果'}</div><div class="result-subtitle">${this.lprSourceLabel(data)}</div></div>`;
    const hero = document.getElementById('lpr-hero');
    const main = data.plates?.[0];
    if (hero) hero.classList.toggle('hidden', !main);
    if (main) {
      document.getElementById('lpr-hero-plate').textContent = this.formatPlateNumber(main.plate_number);
      const cls = this.plateColorClass(main.plate_color);
      document.getElementById('lpr-hero-meta').innerHTML = `<span class="plate-badge ${cls}">${main.plate_color || '蓝牌'}</span> ${this.formatPlateNumber(main.plate_number)} · 置信度 ${((main.confidence || 0) * 100).toFixed(0)}%`;
      const fill = hero.querySelector('.hero-conf-fill');
      if (fill) fill.style.width = `${Math.min(100, (main.confidence || 0) * 100)}%`;
    }
    const plateList = document.getElementById('lpr-plates');
    if (plateList) plateList.innerHTML = (data.plates || []).map(p => `<div class="plate-item"><span class="number">${this.formatPlateNumber(p.plate_number)}</span><span class="color ${this.plateColorClass(p.plate_color)}">${p.plate_color || '蓝牌'}</span></div>`).join('') || '<p style="color:var(--text-muted)">未检测到车牌</p>';
  },

  clearLprDisplay() {
    const preview = document.getElementById('lpr-preview');
    const videoResult = document.getElementById('lpr-video-result');
    const plateTarget = document.getElementById('lpr-video-plates');
    const imgResult = document.getElementById('lpr-image-result');
    const hero = document.getElementById('lpr-hero');
    const batchNav = document.getElementById('lpr-batch-nav');
    if (preview) preview.removeAttribute('src');
    if (videoResult) videoResult.innerHTML = '';
    if (plateTarget) plateTarget.innerHTML = '';
    if (imgResult) imgResult.innerHTML = '';
    if (hero) hero.classList.add('hidden');
    if (batchNav) batchNav.innerHTML = '';
    this.uploadedRecognitionResults = [];
    this._batchSelectedIndex = 0;
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
    video.onerror = () => update(`预览错误：${video.complete ? 'stream unavailable' : 'load failed'}`);
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
      const res = await fetch(this.apiUrl('/api/lpr/rtsp/start'), {
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
      this.lprVideoWs = new WebSocket(`${this.wsBase()}/ws/stream/lpr`);
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
            videoResult.innerHTML = `<div class="result-banner ${data.plate_count ? 'success' : 'danger'}"><div class="result-title">${title}</div><div class="result-subtitle">${subtitle}</div><div class="result-subtitle">backend / RPNet · 帧 ${data.frame ?? frameCount}</div></div>`;
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
              source: 'model',
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
            const resp = await fetch(this.apiUrl('/api/lpr/video-history'), {
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
      await fetch(this.apiUrl('/api/lpr/rtsp/stop'), {
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
    if (this.streamTimeout) { clearTimeout(this.streamTimeout); this.streamTimeout = null; }
    if (this.wsStream) { this.wsStream.close(); this.wsStream = null; }
    this.streamBusy = false;
    const uploadVideo = document.getElementById('police-upload-preview');
    if (uploadVideo && !uploadVideo.hidden) uploadVideo.pause();
    const uploadPlay = document.getElementById('police-upload-play');
    if (uploadPlay) uploadPlay.textContent = '播放视频';
    const streamPreview = document.getElementById('police-stream-preview');
    if (streamPreview) {
      streamPreview.hidden = true;
      streamPreview.removeAttribute('src');
    }
    if (this.streamModule) {
      const video = document.getElementById(this.streamModule + '-video');
      if (video?.srcObject) { video.srcObject.getTracks().forEach(t => t.stop()); video.srcObject = null; }
      if (video) video.hidden = true;
      this.streamModule = null;
    }
  },

  async loadAlerts() {
    try {
      const params = new URLSearchParams({ limit: '50' });
      const level = document.getElementById('alert-filter-level')?.value;
      const status = document.getElementById('alert-filter-status')?.value;
      const eventType = document.getElementById('alert-filter-type')?.value;
      const start = document.getElementById('alert-filter-start')?.value;
      const end = document.getElementById('alert-filter-end')?.value;
      if (level) params.set('level', level);
      if (status) params.set('status', status);
      if (eventType) params.set('event_type', eventType);
      if (start) params.set('start_time', `${start}T00:00:00`);
      if (end) params.set('end_time', `${end}T23:59:59`);
      const [stats, alerts, analytics] = await Promise.all([
        this.api('/api/monitor/alerts/stats'),
        this.api('/api/monitor/alerts?' + params.toString()),
        this.api('/api/monitor/alerts/analytics?days=7'),
      ]);
      document.getElementById('alert-stats').innerHTML = [
        ['总数', stats.total || 0], ['今日', stats.today_count || 0], ['未处理', stats.open || 0], ['处理率', `${Math.round(stats.resolution_rate || 0)}%`],
      ].map(([label, value]) => `<div class="stat-card"><div class="stat-num">${value}</div><div class="stat-label">${label}</div></div>`).join('');
      document.getElementById('alert-analytics').innerHTML = `<p>近 7 天告警：${analytics.total || 0}</p><p>平均处理时间：${analytics.mttr_minutes ?? '--'} 分钟</p><p>主要类型：${(analytics.by_type_ranked || []).slice(0, 3).map(x => `${x.name || x.event_type} ${x.count}`).join('、') || '暂无'}</p>`;
      document.getElementById('alert-timeline').innerHTML = alerts.map(a => {
        const severity = a.severity_assessment || a.detail?.structured?.severity_assessment || {};
        const impact = a.impact_scope || a.detail?.structured?.impact_scope || '暂未发现明确影响范围';
        const occurred = a.occurred_at || a.detail?.structured?.occurred_at || new Date(a.created_at).toLocaleString();
        return `<article class="timeline-item ${this.escHtml(a.level)}">
          <div class="alert-title-row"><strong>${this.escHtml(a.title)}</strong><span class="monitor-pill ${this.escHtml(a.level)}">${this.escHtml(a.level_cn || this.monitorLevelLabel(a.level))}</span></div>
          <p>${this.escHtml(a.summary)}</p>
          <div class="alert-structured-grid">
            <div><b>发生时间</b><span>${this.escHtml(occurred)}</span></div>
            <div><b>影响范围</b><span>${this.escHtml(impact)}</span></div>
            <div><b>可能根因</b><span>${this.escHtml(a.root_cause || '等待进一步分析')}</span></div>
            <div><b>严重度依据</b><span>${this.escHtml(severity.summary_text || severity.decision_reason || '按规则引擎判定')}</span></div>
          </div>
          ${a.suggestion ? `<p class="alert-suggestion"><b>处置建议：</b>${this.escHtml(a.suggestion)}</p>` : ''}
          <small>${this.escHtml(a.status_cn || (a.status === 'resolved' ? '已处理' : '未处理'))} · ${this.escHtml(a.event_type_cn || a.event_type)}</small>
          <div class="alert-actions">
            <button class="btn" onclick="App.viewAlertReplay(${a.id})">事件回放</button>
            <button class="btn" onclick="App.focusAlert(${a.id}, '${this.escHtml(a.title).replace(/'/g, '&#39;')}')">围绕此告警提问</button>
            ${a.status !== 'resolved' ? `<button class="btn" onclick="App.resolveAlert(${a.id})">标记已处理</button>` : ''}
          </div>
        </article>`;
      }).join('') || '<p>暂无告警</p>';
    } catch (e) {
      document.getElementById('alert-timeline').innerHTML = `<p>加载告警失败：${this.escHtml(e.message)}</p>`;
    }
  },

  async loadAlertTypes() {
    try {
      const types = await this.api('/api/monitor/alerts/event-types');
      const select = document.getElementById('alert-filter-type');
      const current = select?.value || '';
      if (select) select.innerHTML = '<option value="">全部事件</option>' + types.map(t => `<option value="${t.key}">${t.name}</option>`).join('');
      if (select) select.value = current;
      const testSelect = document.getElementById('test-alert-type');
      if (testSelect) testSelect.innerHTML = '<option value="">通用测试告警</option>' + types.map(t => `<option value="${t.key}">${t.name}</option>`).join('');
    } catch (e) {}
  },

  connectAlertSse() {
    if (this.alertSse) return;
    this.alertSse = new EventSource(this.apiUrl('/api/monitor/stream'));
    this.alertSse.onmessage = event => {
      const data = JSON.parse(event.data || '{}');
      if (data.type === 'alert') { this.showToast(data); this.loadAlerts(); }
    };
    this.alertSse.onerror = () => { this.alertSse?.close(); this.alertSse = null; };
  },

  async resolveAlert(id) {
    await this.api(`/api/monitor/alerts/${id}/resolve`, { method: 'POST', body: JSON.stringify({ resolution_note: 'Web 页面手动处理' }) });
    this.loadAlerts();
  },

  async viewAlertReplay(id) {
    try {
      const data = await this.api(`/api/monitor/alerts/${id}/replay`);
      const box = document.getElementById('alert-replay');
      const cause = data.cause_analysis || {};
      const events = data.timeline_events || [];
      box.classList.remove('hidden');
      box.innerHTML = `<h3>告警回放 #${id}</h3>
        <div class="replay-summary"><p><b>主要原因：</b>${this.escHtml(cause.primary_cause || '等待进一步分析')}</p><p><b>影响：</b>${this.escHtml(cause.impact_scope || cause.impact || '暂无明确影响')}</p></div>
        <div class="replay-events">${events.map((event, index) => `<div class="replay-event"><b>${index + 1}. ${this.escHtml(event.title || event.type)}</b><span>${this.escHtml(event.time || '')}</span>${event.description ? `<p>${this.escHtml(event.description)}</p>` : ''}</div>`).join('') || '<p>暂无关联事件</p>'}</div>
        <button class="btn" onclick="this.parentElement.classList.add('hidden')">关闭</button>`;
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) { alert(e.message); }
  },

  focusAlert(id, title) {
    this.focusedAlertId = id;
    this.focusedAlertTitle = title || `告警 #${id}`;
    const context = document.getElementById('assistant-context');
    if (context) context.textContent = `当前讨论：${this.focusedAlertTitle}（#${id}）`;
  },

  clearFocusedAlert() {
    this.focusedAlertId = null;
    this.focusedAlertTitle = '';
    const context = document.getElementById('assistant-context');
    if (context) context.textContent = '尚未选定具体告警；点击时间线中的“围绕此告警提问”。';
  },

  async loadAgentBriefing() {
    try {
      const data = await this.api('/api/monitor/agent/briefing');
      document.getElementById('agent-briefing').textContent = data.briefing || data.summary || JSON.stringify(data);
    } catch (e) {}
  },

  async askMonitorAssistant(intent = null, presetQuestion = '') {
    const input = document.getElementById('assistant-question');
    const output = document.getElementById('assistant-answer');
    const question = presetQuestion || input?.value.trim();
    if (!question) return;
    if (presetQuestion && input) input.value = presetQuestion;
    output.textContent = '分析中…';
    try {
      const payload = { question };
      if (intent) payload.intent = intent;
      if (this.focusedAlertId) payload.alert_id = this.focusedAlertId;
      const data = await this.api('/api/monitor/assistant', { method: 'POST', body: JSON.stringify(payload) });
      output.textContent = data.answer || JSON.stringify(data);
    } catch (e) { output.textContent = e.message; }
  },

  async testAlert() {
    try {
      const eventType = document.getElementById('test-alert-type')?.value;
      const path = eventType ? `/api/monitor/alerts/test/${encodeURIComponent(eventType)}` : '/api/monitor/alerts/test';
      const data = await this.api(path, { method: 'POST' });
      this.showToast({ level: data.level || 'info', title: data.title, summary: data.summary });
      this.loadAlerts();
    } catch (e) { alert(e.message); }
  },

  async loadMonitorDiagnostics() {
    const box = document.getElementById('monitor-diagnostics');
    if (!box) return;
    try {
      const [connections, config, tokens] = await Promise.all([
        this.api('/api/monitor/connections'), this.api('/api/monitor/config'), this.api('/api/monitor/token-usage'),
      ]);
      box.innerHTML = `<div><b>实时连接</b><span>WebSocket ${connections.websocket_clients || 0} · 告警 SSE ${connections.sse_clients || 0} · 日志 SSE ${connections.log_sse_clients || 0}</span></div>
        <div><b>LLM</b><span>${this.escHtml(config.llm_provider_label || config.llm_provider || '模板模式')} · ${this.escHtml(config.llm_model || '')} · Token ${tokens.used || 0}/${tokens.limit || 0}</span></div>
        <div><b>通知渠道</b><span>Webhook ${config.webhook_enabled ? '已开启' : '已关闭'} · 邮件 ${config.email_enabled ? '已开启' : '已关闭'} · SSE ${config.sse_enabled === false ? '已关闭' : '已开启'}</span></div>`;
    } catch (e) { box.textContent = `诊断信息加载失败：${e.message}`; }
  },

  async testMonitorNotification(channel) {
    try {
      const data = await this.api(`/api/monitor/notifications/test?channel=${encodeURIComponent(channel)}`, { method: 'POST' });
      alert(data.message || `${channel} 测试完成`);
      this.loadMonitorDiagnostics();
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
    const level = document.getElementById('log-level')?.value || '';
    const search = document.getElementById('log-search')?.value.trim() || '';
    const userId = document.getElementById('log-user')?.value || '';
    const start = document.getElementById('log-start')?.value || '';
    const end = document.getElementById('log-end')?.value || '';
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (cat) params.set('category', cat);
      if (level) params.set('level', level);
      if (search) params.set('search', search);
      if (userId) params.set('user_id', userId);
      if (start) params.set('start', start);
      if (end) params.set('end', end);
      const data = await this.api('/api/monitor/logs?' + params.toString());
      document.getElementById('log-table').innerHTML =
        '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span></div>' +
        (data.map(l => {
          const label = l.level_cn || this.monitorLevelLabel(l.level);
          const category = l.category_cn || this.monitorCategoryLabel(l.category);
          const message = l.display_message || l.message;
          const detail = l.detail_json && typeof l.detail_json === 'object' ? JSON.stringify(l.detail_json, null, 2) : '';
          return `<details class="log-entry"><summary class="log-row severity-${this.escHtml(l.level)}"><span>${new Date(l.created_at).toLocaleString()}</span><span class="monitor-pill">${this.escHtml(label)}</span><span>${this.escHtml(category)}</span><span>${this.escHtml(message)}</span></summary>${detail ? `<pre class="log-detail">${this.escHtml(detail)}</pre>` : ''}</details>`;
        }).join('') || '<p>暂无日志</p>');
    } catch (e) {
      document.getElementById('log-table').innerHTML = `<p>加载日志失败: ${e.message}</p>`;
    }
  },

  async loadLogStats() {
    try {
      const hours = document.getElementById('log-stats-hours')?.value || '24';
      const data = await this.api(`/api/monitor/logs/stats?hours=${hours}`);
      const rows = [[`${hours}h 总数`, data.total || 0], ...Object.entries(data.by_level || {})];
      document.getElementById('log-stats').innerHTML = rows.map(([label, value]) => `<div class="stat-card"><div class="stat-num">${value}</div><div class="stat-label">${label}</div></div>`).join('');
    } catch (e) {}
  },

  toggleLogStream() {
    const button = document.getElementById('log-stream-btn');
    if (this.logSse) {
      this.logSse.close();
      this.logSse = null;
      if (button) button.textContent = '开启实时日志';
      const status = document.getElementById('log-stream-status');
      if (status) status.textContent = '未连接';
      return;
    }
    this.logSse = new EventSource(this.apiUrl('/api/monitor/logs/stream'));
    this.logSse.onopen = () => { const status = document.getElementById('log-stream-status'); if (status) status.textContent = '已连接'; };
    this.logSse.onmessage = () => { this.loadLogs(); this.loadLogStats(); };
    this.logSse.onerror = () => { this.logSse?.close(); this.logSse = null; if (button) button.textContent = '开启实时日志'; const status = document.getElementById('log-stream-status'); if (status) status.textContent = '连接中断'; };
    if (button) button.textContent = '停止实时日志';
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
