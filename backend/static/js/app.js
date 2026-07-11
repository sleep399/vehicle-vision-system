const App = {
  token: localStorage.getItem('token') || '',
  streamModule: null,
  streamInterval: null,
  wsAlerts: null,
  wsStream: null,
  assistantHistory: [],
  assistantThinking: false,
  assistantRecognition: null,
  assistantVoiceEnabled: false,
  _agentVoice: null,
  _agentSpeaking: false,
  alertTypes: [],
  currentReplayId: null,
  alertTimelineSkip: 0,
  alertTimelineHasMore: false,
  replayEvents: [],
  replayStepIndex: 0,
  replayPlayTimer: null,
  agentOpenCount: 0,
  _recentAlertKeys: {},
  agentDragMoved: false,
  agentSpeechTimer: null,
  _agentPointerId: null,
  agentMonitorTimer: null,
  agentBriefing: null,
  agentLastBriefKey: '',
  focusedAlert: null,
  currentView: '',
  logSseSource: null,
  LOG_CATEGORY_LABELS: {
    lpr: '车牌识别',
    police_gesture: '交警手势',
    owner_gesture: '车主手势',
    alert: '告警',
    user: '用户操作',
    system: '系统运行',
    agent: '智能体决策',
  },
  LOG_CATEGORY_COLORS: {
    lpr: '#6A9BCC',
    police_gesture: '#C9943A',
    owner_gesture: '#6B8F47',
    alert: '#C0453A',
    user: '#8B7EC8',
    system: '#64748b',
    agent: '#0891b2',
  },

  /** 构建助手请求：仅携带用户显式选定的告警，不再静默绑定「最近一条」 */
  buildAssistantPayload(question) {
    const body = { question };
    const alertId = this.getExplicitAlertId();
    if (alertId) body.alert_id = alertId;
    return body;
  },

  /** 当前显式选定的告警 ID（仅以 focusedAlert 为准；回放页打开不等于助手上下文） */
  getExplicitAlertId() {
    return this.focusedAlert?.id ?? null;
  },

  /** 用户显式选定某条告警作为对话上下文 */
  setFocusedAlert(alert) {
    if (!alert || !alert.id) return;
    this.focusedAlert = {
      id: alert.id,
      title: alert.title || '系统提醒',
      level: alert.level || 'info',
    };
    this.updateAssistantContextUI();
  },

  /** 取消当前选定的告警上下文（不影响告警回放面板本身） */
  clearFocusedAlert() {
    this.focusedAlert = null;
    this.updateAssistantContextUI();
  },

  updateAssistantContextUI() {
    const bar = document.getElementById('assistant-context-bar');
    const titleEl = document.getElementById('assistant-context-title');
    const subtitle = document.getElementById('agent-subtitle');

    if (this.focusedAlert && bar && titleEl) {
      bar.classList.remove('hidden');
      titleEl.textContent = this.focusedAlert.title;
      if (subtitle) subtitle.textContent = `正在讨论：${this.focusedAlert.title}`;
      return;
    }

    if (bar) bar.classList.add('hidden');
    if (titleEl) titleEl.textContent = '';
    if (subtitle && !this.agentOpenCount) {
      subtitle.textContent = '感知 · 决策 · 告警推送';
    } else if (subtitle && this.agentOpenCount > 0) {
      subtitle.textContent = `发现 ${this.agentOpenCount} 条未处理告警（请先选定一条再提问）`;
    }
  },

  /**
   * 快捷提问（根因/建议/升级/影响）
   * requireAlert=true 时若无选定告警，由后端返回「您指的是哪条告警？」
   */
  askAboutAlert(question) {
    this.askAssistant(question);
  },

  /** 用户能听懂的简短告警话术 */
  alertToUserSpeech(alert) {
    const title = alert.title || '系统提醒';
    const summary = alert.summary || '';
    if (summary && summary.length < 60) return summary;
    return title;
  },

  init() {
    this.bindTabs();
    this.bindNav();
    this.bindFileInputs();
    this.initAssistant();
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
    if (this.token) {
      fetch('/api/auth/logout', { method: 'POST', headers: this.headers() }).catch(() => {});
    }
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
      if (data.type === 'alert') this.handleIncomingAlert(data);
    };
      this.wsAlerts.onopen = () => { var el = document.getElementById('stat-ws-conn'); if (el) el.textContent = '1'; };
      this.wsAlerts.onclose = () => {
        this.wsAlerts = null;
        var _el = document.getElementById('stat-ws-conn'); if (_el) _el.textContent = '0';
      setTimeout(() => { if (document.getElementById('main-page').classList.contains('active')) this.connectAlertWs(); }, 3000);
    };
  },

  connectSSE() {
    if (this.sseSource) return;
    this.sseSource = new EventSource('/api/monitor/stream');
    this.sseSource.onopen = () => {
      document.getElementById('stat-sse-conn') && (document.getElementById('stat-sse-conn').textContent = '1');
    };
    this.sseSource.addEventListener('connected', (e) => {
      console.log('SSE connected:', JSON.parse(e.data));
    });
    this.sseSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'alert') this.handleIncomingAlert(data);
      } catch (err) {}
    };
    this.sseSource.onerror = () => {
      document.getElementById('stat-sse-conn') && (document.getElementById('stat-sse-conn').textContent = '0');
    };
  },

  disconnectSSE() {
    if (this.sseSource) { this.sseSource.close(); this.sseSource = null; }
    var ssc2 = document.getElementById('stat-sse-conn'); if (ssc2) ssc2.textContent = '0';
  },

  handleIncomingAlert(alert) {
    if (!alert) return;
    const now = Date.now();
    const key = alert.id
      ? `id:${alert.id}`
      : `et:${alert.event_type || ''}:${alert.title || ''}`;
    const last = this._recentAlertKeys[key] || 0;
    if (now - last < 60000) return;
    this._recentAlertKeys[key] = now;
    Object.keys(this._recentAlertKeys).forEach(k => {
      if (now - this._recentAlertKeys[k] > 120000) delete this._recentAlertKeys[k];
    });
    this.showToast(alert);
    this.prependLiveAlert(alert);
    this.onAgentAlert(alert);
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
    const endpoints = { lpr: '/api/lpr/recognize', police: '/api/police-gesture/recognize', owner: '/api/owner-gesture/recognize' };
    const form = new FormData();
    form.append('file', file);
    const headers = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    const previewMap = { lpr: 'lpr-preview', police: 'police-preview', owner: 'owner-preview' };
    const resultMap = { lpr: 'lpr-results', police: 'police-result', owner: 'owner-result' };
    const preview = document.getElementById(previewMap[module]);
    const resultBox = document.getElementById(resultMap[module]);
    if (preview && file.type.startsWith('image/')) preview.src = URL.createObjectURL(file);
    if (resultBox) resultBox.innerHTML = '正在识别，请稍候...';
    try {
      const res = await fetch(endpoints[module], { method: 'POST', body: form, headers });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '识别失败');
      this.renderResult(module, data);
      if (module === 'owner' && data.action) this.loadVehicleState();
    } catch (e) {
      if (resultBox) resultBox.innerHTML = `识别失败：${e.message}`;
      alert(e.message);
    }
  },

  renderResult(module, data) {
    if (module === 'lpr') {
      document.getElementById('lpr-preview').src = 'data:image/jpeg;base64,' + data.annotated_image;
      document.getElementById('lpr-results').innerHTML = `
        <div class="result-banner ${data.success ? 'success' : 'danger'}">
          <div class="result-title">${data.success ? '识别成功' : '未识别到有效车牌'}</div>
          <div class="result-subtitle">共检测到 ${data.plate_count} 个车牌</div>
        </div>`;
      const plateColorClass = {
        '蓝牌': 'plate-blue', '绿牌': 'plate-green', '黄牌': 'plate-yellow',
        '白牌': 'plate-white', '黑牌': 'plate-black',
      };
      document.getElementById('lpr-plates').innerHTML = data.plates.map(p =>
        `<div class="plate-item"><span class="number">${p.plate_number}</span><span class="color ${plateColorClass[p.plate_color] || ''}">${p.plate_color} (${(p.confidence*100).toFixed(0)}%)</span></div>`
      ).join('') || '<p>未检测到车牌</p>';
      this.loadLprHistory();
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

  async loadAlertTypes() {
    try {
      const types = await this.api('/api/monitor/alerts/event-types');
      this.alertTypes = types;
      const filterSelect = document.getElementById('alert-filter-type');
      const testSelect = document.getElementById('test-alert-type');
      const options = types.map(t => `<option value="${t.key}">${t.name} (${t.default_level})</option>`).join('');
      if (filterSelect) filterSelect.innerHTML = '<option value="">全部类型</option>' + options;
      if (testSelect) testSelect.innerHTML = '<option value="">选择测试类型</option>' + options;
    } catch (e) {}
  },

  async triggerTypeAlert() {
    const sel = document.getElementById('test-alert-type');
    if (!sel || !sel.value) return;
    try {
      const data = await this.api(`/api/monitor/alerts/test/${sel.value}`, { method: 'POST' });
      this.showToast({ level: data.level, title: data.title, summary: data.summary });
      this.loadAlerts();
      this.loadAlertAnalytics();
      this.loadAgentActivity();
    } catch (e) { alert(e.message); }
  },

  async loadAgentActivity() {
    const el = document.getElementById('agent-activity');
    if (!el) return;
    try {
      const list = await this.api('/api/monitor/logs?category=agent&limit=15');
      if (!list.length) {
        el.innerHTML = '<p style="color:var(--text-muted)">暂无智能体日志，触发识别或告警后会自动记录</p>';
        return;
      }
      el.innerHTML = list.map(log => {
        const time = log.created_at ? new Date(log.created_at).toLocaleString() : '';
        const levelClass = (log.level || 'INFO').toLowerCase();
        return `<div class="agent-activity-item ${levelClass}">
          <span class="agent-activity-time">${time}</span>
          <span class="agent-activity-level">${log.level || 'INFO'}</span>
          <span class="agent-activity-msg">${this.escHtml(log.message || '')}</span>
        </div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = '<p style="color:var(--text-muted)">加载失败</p>';
    }
  },

  async cleanupNoiseAlerts() {
    if (!confirm('将测试告警和可选配置缺失类历史告警标记为已处理，是否继续？')) return;
    try {
      const data = await this.api('/api/monitor/alerts/cleanup-noise', { method: 'POST' });
      alert(`已清理 ${data.resolved} 条噪声告警`);
      this.loadAlerts();
      this.loadAlertAnalytics();
    } catch (e) { alert(e.message); }
  },

  async loadAlertNotifications() {
    const el = document.getElementById('alert-notifications');
    if (!el) return;
    try {
      const conn = await this.api('/api/monitor/connections');
      const cfg = await this.api('/api/monitor/config');
      el.innerHTML = `
        <div class="notification-card on"><strong>WebSocket</strong><span>${conn.websocket_clients} 个在线连接</span></div>
        <div class="notification-card ${cfg.sse_enabled ? 'on' : 'off'}"><strong>SSE 实时推送</strong><span>${cfg.sse_enabled ? '已启用' : '未启用'}</span></div>
        <div class="notification-card ${cfg.webhook_enabled ? 'on' : 'off'}"><strong>Webhook</strong><span>${cfg.webhook_enabled ? (cfg.webhook_url_configured ? '已启用' : '已启用但未配置 URL') : '未启用'}</span></div>
        <div class="notification-card ${cfg.email_enabled ? 'on' : 'off'}"><strong>邮件通知</strong><span>${cfg.email_enabled ? (cfg.email_configured ? '已启用' : '已启用但 SMTP 不完整') : '未启用'}</span></div>
      `;
    } catch (e) {
      el.innerHTML = '<p style="color:var(--text-muted)">通知状态加载失败</p>';
    }
  },

  async testNotifications(channel) {
    try {
      const data = await this.api(`/api/monitor/notifications/test?channel=${encodeURIComponent(channel || 'all')}`, { method: 'POST' });
      const lines = Object.entries(data.channels || {}).map(([name, result]) => {
        if (typeof result === 'object' && result !== null) {
          if (result.ok) return `${name}: 成功`;
          return `${name}: 失败${result.reason ? `（${result.reason}）` : ''}`;
        }
        return `${name}: ${result ? '成功' : '失败'}`;
      });
      alert('通知测试完成\n\n' + (lines.join('\n') || '无可用渠道'));
      this.loadAlertNotifications();
    } catch (e) { alert(e.message); }
  },

  async loadAlertConfig() {
    const el = document.getElementById('alert-config');
    if (!el) return;
    try {
      const cfg = await this.api('/api/monitor/config');
      el.innerHTML = `
        <div class="config-grid">
          <div><span>连续失败阈值</span><strong>${cfg.failure_threshold}</strong></div>
          <div><span>滑窗秒数</span><strong>${cfg.window_seconds}</strong></div>
          <div><span>冷却秒数</span><strong>${cfg.cooldown_seconds}</strong></div>
          <div><span>低置信度阈值</span><strong>${cfg.low_confidence_threshold}</strong></div>
          <div><span>Token 上限</span><strong>${cfg.token_limit}</strong></div>
          <div><span>LLM 模型</span><strong>${cfg.llm_model || '模板降级'}</strong></div>
          <div><span>LLM 状态</span><strong>${cfg.llm_configured ? '已配置' : '未配置（模板告警）'}</strong></div>
          <div><span>巡检周期</span><strong>60 秒</strong></div>
        </div>`;
    } catch (e) {
      el.innerHTML = '<p style="color:var(--text-muted)">配置加载失败</p>';
    }
  },

  // ── 系统日志 ──
  onLogDatetimeChange(el) {
    if (!el) return;
    el.classList.toggle('has-value', !!el.value);
  },

  syncLogDatetimeState() {
    ['log-start', 'log-end'].forEach(id => {
      const el = document.getElementById(id);
      if (el) this.onLogDatetimeChange(el);
    });
  },

  initSelectChevrons() {
    document.querySelectorAll('.select-input-wrap select').forEach(select => {
      const wrap = select.closest('.select-input-wrap');
      if (!wrap || wrap.dataset.chevronBound) return;
      wrap.dataset.chevronBound = '1';
      const close = () => wrap.classList.remove('is-open');
      select.addEventListener('mousedown', () => wrap.classList.add('is-open'));
      select.addEventListener('blur', close);
      select.addEventListener('change', close);
    });
  },

  resetLogFilters(reload = true) {
    const ids = ['log-category', 'log-level', 'log-search', 'log-user', 'log-start', 'log-end'];
    const defaults = { 'log-category': '', 'log-level': '', 'log-search': '', 'log-user': '', 'log-start': '', 'log-end': '' };
    ids.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = defaults[id] ?? '';
    });
    this.syncLogDatetimeState();
    if (reload) this.loadLogs();
  },

  logCategoryLabel(category) {
    return this.LOG_CATEGORY_LABELS[category] || category;
  },

  getLogFilterParams() {
    return {
      cat: (document.getElementById('log-category') && document.getElementById('log-category').value) || '',
      level: (document.getElementById('log-level') && document.getElementById('log-level').value) || '',
      search: (document.getElementById('log-search') && document.getElementById('log-search').value) || '',
      userId: (document.getElementById('log-user') && document.getElementById('log-user').value) || '',
      start: (document.getElementById('log-start') && document.getElementById('log-start').value) || '',
      end: (document.getElementById('log-end') && document.getElementById('log-end').value) || '',
    };
  },

  logMatchesFilters(log, filters) {
    if (!log) return false;
    if (filters.cat && log.category !== filters.cat) return false;
    if (filters.level && log.level !== filters.level) return false;
    if (filters.userId && String(log.user_id || '') !== String(filters.userId)) return false;
    if (filters.search) {
      const q = filters.search.toLowerCase();
      const msg = String(log.message || '').toLowerCase();
      if (!msg.includes(q)) return false;
    }
    if (filters.start) {
      const ts = new Date(log.created_at).getTime();
      if (ts < new Date(filters.start).getTime()) return false;
    }
    if (filters.end) {
      const ts = new Date(log.created_at).getTime();
      if (ts > new Date(filters.end).getTime()) return false;
    }
    return true;
  },

  renderLogRow(log, live) {
    const liveClass = live ? ' log-row-live' : '';
    return `
      <div class="log-row${liveClass}" onclick="App.showLogDetail('${this.escAttr(JSON.stringify(log))}')">
        <span>${new Date(log.created_at).toLocaleString()}</span>
        <span class="level-${log.level}">${log.level}</span>
        <span>${this.escHtml(this.logCategoryLabel(log.category))}</span>
        <span>${this.escHtml(log.message)}</span>
        <span>${log.user_id || '-'}</span>
      </div>`;
  },

  renderLogTable(logs) {
    const table = document.getElementById('log-table');
    if (!table) return;
    const header = '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span><span>用户</span></div>';
    const rows = (logs || []).map(l => this.renderLogRow(l, false)).join('');
    table.innerHTML = header + (rows || '<p style="padding:1rem;color:var(--text-muted);">暂无符合条件的日志</p>');
  },

  prependLiveLog(log) {
    if (!this.logMatchesFilters(log, this.getLogFilterParams())) return;
    const table = document.getElementById('log-table');
    if (!table) return;
    const empty = table.querySelector('p');
    if (empty) empty.remove();
    if (!table.querySelector('.log-row.header')) {
      table.innerHTML = '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span><span>用户</span></div>';
    }
    const header = table.querySelector('.log-row.header');
    header.insertAdjacentHTML('afterend', this.renderLogRow(log, true));
    const rows = table.querySelectorAll('.log-row:not(.header)');
    if (rows.length > 100) rows[rows.length - 1].remove();
    setTimeout(() => {
      const first = table.querySelector('.log-row-live');
      if (first) first.classList.remove('log-row-live');
    }, 2500);
    this.loadLogStats();
  },

  renderLogStats(stats) {
    const statsPanel = document.getElementById('log-stats-panel');
    const statsEl = document.getElementById('log-stats');
    const chartsEl = document.getElementById('log-charts');
    if (!statsPanel || !statsEl || !chartsEl || !stats) return;
    statsPanel.style.display = 'block';

    const catHtml = Object.entries(stats.by_category || {}).map(([k, v]) =>
      `<span class="badge">${this.escHtml(this.logCategoryLabel(k))}: ${v}</span>`
    ).join('');
    statsEl.innerHTML = `
      <span>${stats.hours || 24}h 总计: <b>${stats.total}</b> 条</span>
      ${catHtml}
      ${Object.entries(stats.by_level || {}).map(([k,v]) => `<span class="badge level-${k}">${k}: ${v}</span>`).join('')}
    `;

    const ranked = stats.category_ranked || [];
    const maxCat = Math.max(...ranked.map(c => c.count), 1);
    let categoryHtml = '<div class="log-chart-panel"><h4>类别分布</h4><div class="dist-bars">';
    for (const item of ranked) {
      const pct = Math.round(item.count / maxCat * 100);
      const color = this.LOG_CATEGORY_COLORS[item.key] || '#9B9890';
      categoryHtml += `<div class="dist-row"><span class="dist-label dist-label-wide">${this.escHtml(item.name)}</span>
        <div class="dist-bar-track"><div class="dist-bar-fill" style="width:${pct}%;background:${color};"></div></div>
        <span class="dist-count">${item.count}</span></div>`;
    }
    categoryHtml += ranked.length ? '</div></div>' : '<p class="log-chart-empty">暂无类别数据</p></div>';

    const hourly = stats.hour_trend || [];
    const maxHour = Math.max(...hourly.map(h => h.count), 1);
    let hourHtml = '<div class="log-chart-panel"><h4>时间趋势</h4><div class="hourly-chart log-hourly-chart">';
    for (const h of hourly) {
      const label = h.hour ? h.hour.slice(11, 16) : '';
      const hPct = Math.max(4, Math.round(h.count / maxHour * 100));
      hourHtml += `<div class="hourly-bar" title="${this.escHtml(h.hour || '')}: ${h.count}条">
        <div class="hourly-fill" style="height:${hPct}%;"></div>
        <span class="hourly-label">${label}</span>
      </div>`;
    }
    hourHtml += hourly.length ? '</div></div>' : '<p class="log-chart-empty">暂无趋势数据</p></div>';

    chartsEl.innerHTML = categoryHtml + hourHtml;
  },

  async loadLogStats() {
    try {
      const hoursEl = document.getElementById('log-stats-hours');
      const hours = hoursEl ? hoursEl.value : 24;
      const stats = await this.api(`/api/monitor/logs/stats?hours=${hours}`);
      this.renderLogStats(stats);
    } catch (e) {}
  },

  connectLogStream() {
    if (this.logSseSource) return;
    const statusEl = document.getElementById('log-stream-status');
    const btn = document.getElementById('log-stream-btn');
    this.logSseSource = new EventSource('/api/monitor/logs/stream');
    this.logSseSource.onopen = () => {
      if (statusEl) { statusEl.textContent = '监听中'; statusEl.className = 'conn-status connected'; }
      if (btn) btn.textContent = '停止监听';
    };
    this.logSseSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'log') this.prependLiveLog(data);
      } catch (err) {}
    };
    this.logSseSource.onerror = () => {
      if (statusEl) { statusEl.textContent = '重连中...'; statusEl.className = 'conn-status reconnecting'; }
    };
  },

  disconnectLogStream() {
    if (this.logSseSource) {
      this.logSseSource.close();
      this.logSseSource = null;
    }
    const statusEl = document.getElementById('log-stream-status');
    const btn = document.getElementById('log-stream-btn');
    if (statusEl) { statusEl.textContent = '未连接'; statusEl.className = 'conn-status'; }
    if (btn) btn.textContent = '实时监听';
  },

  toggleLogStream() {
    if (this.logSseSource) this.disconnectLogStream();
    else this.connectLogStream();
  },

  async loadLogs() {
    try {
      const filters = this.getLogFilterParams();

      let url = '/api/monitor/logs?limit=100';
      if (filters.cat) url += '&category=' + filters.cat;
      if (filters.level) url += '&level=' + filters.level;
      if (filters.search) url += '&search=' + encodeURIComponent(filters.search);
      if (filters.userId) url += '&user_id=' + filters.userId;
      if (filters.start) url += '&start=' + new Date(filters.start).toISOString();
      if (filters.end) url += '&end=' + new Date(filters.end).toISOString();

      const data = await this.api(url);
      this.renderLogTable(data);
      await this.loadLogStats();
    } catch (e) {
      document.getElementById('log-table').innerHTML = `<p style="padding:1rem;color:var(--danger);">加载日志失败: ${e.message}</p>`;
    }
  },

  showLogDetail(jsonStr) {
    try {
      const log = JSON.parse(jsonStr);
      let detailHtml = '';
      if (log.detail_json && typeof log.detail_json === 'object') {
        detailHtml = `<pre class="replay-detail" style="max-height:200px;overflow-y:auto;">${JSON.stringify(log.detail_json, null, 2)}</pre>`;
      }
      alert(`日志详情\n\n时间: ${new Date(log.created_at).toLocaleString()}\n级别: ${log.level}\n类别: ${log.category}\n消息: ${log.message}\n${detailHtml ? '详情: 见下方' : ''}`);
    } catch (e) {}
  },

  exportLogs(format) {
    const rows = document.querySelectorAll('#log-table .log-row:not(.header)');
    if (rows.length === 0) { alert('没有可导出的日志'); return; }
    const data = [];
    rows.forEach(r => {
      const cells = r.querySelectorAll('span');
      data.push({
        time: cells[0] ? cells[0].textContent : '',
        level: cells[1] ? cells[1].textContent : '',
        category: cells[2] ? cells[2].textContent : '',
        message: cells[3] ? cells[3].textContent : '',
        user: cells[4] ? cells[4].textContent : '-',
      });
    });
    let content, mime, ext;
    if (format === 'csv') {
      content = '时间,级别,类别,消息,用户\n' + data.map(d => `"${d.time}","${d.level}","${d.category}","${d.message}","${d.user}"`).join('\n');
      mime = 'text/csv'; ext = 'csv';
    } else {
      content = JSON.stringify(data, null, 2);
      mime = 'application/json'; ext = 'json';
    }
    const blob = new Blob(['\uFEFF' + content], { type: mime + ';charset=utf-8' });
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = `logs_${new Date().toISOString().slice(0,10)}.${ext}`;
    a.click();
  },

  healthLabel(status) {
    const labels = { healthy: '健康', warning: '警告', critical: '严重', unknown: '未知', error: '异常' };
    return labels[status] || status || '-';
  },

  escHtml(text) {
    if (text == null) return '';
    return String(text).replace(/[&<>'"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[m]));
  },

  /** 助手气泡：转义 HTML 并渲染基础 Markdown（**加粗**、换行） */
  formatAssistantText(text) {
    if (text == null) return '';
    let s = this.escHtml(text);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\n/g, '<br>');
    return s;
  },

  stripMarkdown(text) {
    if (!text) return '';
    return String(text)
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '$1')
      .replace(/^#+\s*/gm, '');
  },

  escAttr(text) {
    if (text == null) return '';
    return String(text).replace(/[&<>'"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[m]));
  },

  // ── 日期时间默认值 ──
  initDatetimeDefaults() {
    // 日志中心默认不按时间筛选，避免「结束时间」停留在页面打开时刻导致新日志被过滤
  },

  // ── 告警智能体可视化 ──
  initAssistant() {
    const input = document.getElementById('assistant-input');
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') this.askAssistant();
      });
    }
    this.renderAssistantHistory();
    this.setAssistantStatus('准备就绪');
  },

  toggleAssistant() {
    const panel = document.getElementById('assistant-panel');
    const toggle = document.getElementById('assistant-toggle');
    if (!panel || !toggle) return;
    const isHidden = panel.classList.toggle('hidden');
    toggle.classList.toggle('active', !isHidden);
    toggle.setAttribute('aria-expanded', String(!isHidden));
    if (!isHidden) {
      this.renderAssistantHistory();
      document.addEventListener('click', this.closeAssistantOnOutsideClick);
    } else {
      document.removeEventListener('click', this.closeAssistantOnOutsideClick);
    }
  },

  closeAssistantOnOutsideClick(evt) {
    const panel = document.getElementById('assistant-panel');
    const toggle = document.getElementById('assistant-toggle');
    if (!panel || !toggle) return;
    if (evt.target instanceof Element && !panel.contains(evt.target) && !toggle.contains(evt.target)) {
      panel.classList.add('hidden');
      toggle.classList.remove('active');
      toggle.setAttribute('aria-expanded', 'false');
      document.removeEventListener('click', App.closeAssistantOnOutsideClick);
    }
  },

  setAssistantStatus(text, isThinking = false) {
    const el = document.getElementById('assistant-status');
    if (!el) return;
    el.classList.toggle('thinking', isThinking);
    el.innerHTML = isThinking ? `<span class="assistant-thinking"><span></span><span></span><span></span></span> ${text}` : text;
  },

  addAssistantMessage(role, content) {
    this.assistantHistory.push({ role, content });
    this.renderAssistantHistory();
  },

  renderAssistantHistory() {
    const box = document.getElementById('assistant-history');
    if (!box) return;
    box.innerHTML = this.assistantHistory.map(msg => `
      <div class="assistant-msg ${msg.role}">
        <div class="assistant-msg-bubble">${this.escapeHtml(msg.content)}</div>
      </div>
    `).join('');
    box.scrollTop = box.scrollHeight;
  },

  escapeHtml(text) {
    return String(text).replace(/[&<>'"]/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[m]));
  },

  async askAssistant(question) {
    const input = document.getElementById('assistant-input');
    const q = typeof question === 'string' ? question : input?.value?.trim();
    if (!q || this.assistantThinking) return;

    this.addAssistantMessage('user', q);
    if (input) input.value = '';
    this.assistantThinking = true;
    this.setAssistantStatus('Alert Agent 正在思考...', true);

    const toggle = document.getElementById('assistant-toggle');
    if (toggle) toggle.classList.add('thinking');
    const panel = document.getElementById('assistant-panel');
    if (panel) panel.classList.add('assistant-processing');

    try {
      const data = await this.api('/api/monitor/assistant', {
        method: 'POST',
        body: JSON.stringify({ question: q, event_type: 'unknown', path: '/api/monitor' }),
      });
      const answer = data.answer || '暂无回答';
      this.addAssistantMessage('assistant', answer);
      this.setAssistantStatus('分析完成，随时可以继续提问');
      if (this.assistantVoiceEnabled) this.speakAssistant(answer);
    } catch (e) {
      this.addAssistantMessage('assistant', `请求失败: ${e.message}`);
      this.setAssistantStatus('请求失败，请稍后重试');
    } finally {
      this.assistantThinking = false;
      const toggle = document.getElementById('assistant-toggle');
      if (toggle) toggle.classList.remove('thinking');
      if (panel) panel.classList.remove('assistant-processing');
    }
  },

  startDrag(evt) {
    const panel = document.getElementById('assistant-panel');
    if (!panel || evt.target.closest('.assistant-close') || evt.target.closest('.assistant-icon-btn') || evt.target.closest('button') || evt.target.closest('input') || evt.target.closest('.assistant-history')) return;
    evt.preventDefault();
    this.dragOffsetX = evt.clientX - panel.getBoundingClientRect().left;
    this.dragOffsetY = evt.clientY - panel.getBoundingClientRect().top;
    panel.classList.add('dragging');

    const onMouseMove = (moveEvt) => {
      panel.style.left = `${moveEvt.clientX - this.dragOffsetX}px`;
      panel.style.top = `${moveEvt.clientY - this.dragOffsetY}px`;
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
    };
    const onMouseUp = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      panel.classList.remove('dragging');
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  },

  startVoiceInput() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      this.setAssistantStatus('当前浏览器不支持语音输入');
      return;
    }
    if (this.assistantRecognition) {
      this.assistantRecognition.stop();
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = 'zh-CN';
    recognition.continuous = false;
    recognition.interimResults = false;
    this.assistantRecognition = recognition;

    const toggle = document.getElementById('assistant-toggle');
    if (toggle) toggle.classList.add('listening');

    recognition.onstart = () => this.setAssistantStatus('正在聆听...', false);
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results).map(r => r[0].transcript).join('');
      const input = document.getElementById('assistant-input');
      if (input) input.value = transcript;
      this.askAssistant(transcript);
    };
    recognition.onerror = (e) => {
      this.setAssistantStatus(`语音输入失败: ${e.error}`);
    };
    recognition.onend = () => {
      this.assistantRecognition = null;
      if (toggle) toggle.classList.remove('listening');
      if (!this.assistantThinking) this.setAssistantStatus('准备就绪');
    };
    recognition.start();
  },

  speakAssistant(text) {
    if (!('speechSynthesis' in window) || !text) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'zh-CN';
    window.speechSynthesis.speak(utterance);
  },

  speakLastAnswer() {
    const last = [...this.assistantHistory].reverse().find(msg => msg.role === 'assistant');
    if (last) this.speakAssistant(last.content);
  },

  async loadLogs() {
    const cat = document.getElementById('log-category')?.value || '';
    const level = document.getElementById('log-level')?.value || '';
    const search = document.getElementById('log-search')?.value || '';
    const userId = document.getElementById('log-user')?.value || '';
    const start = document.getElementById('log-start')?.value || '';
    const end = document.getElementById('log-end')?.value || '';
    try {
      let url = '/api/monitor/logs?limit=100';
      if (cat) url += '&category=' + encodeURIComponent(cat);
      if (level) url += '&level=' + encodeURIComponent(level);
      if (search) url += '&search=' + encodeURIComponent(search);
      if (userId) url += '&user_id=' + encodeURIComponent(userId);
      if (start) url += '&start=' + encodeURIComponent(new Date(start).toISOString());
      if (end) url += '&end=' + encodeURIComponent(new Date(end).toISOString());

      const data = await this.api(url);
      const header = '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span></div>';
      const rows = (data || []).map(l =>
        `<div class="log-row"><span>${new Date(l.created_at).toLocaleString()}</span><span class="level-${l.level}">${l.level}</span><span>${l.category}</span><span>${l.message}</span></div>`
      ).join('');
      document.getElementById('log-table').innerHTML = header + (rows || '<p style="padding:1rem;color:var(--text-muted);">暂无符合条件的日志</p>');
    } catch (e) {
      document.getElementById('log-table').innerHTML = `<p style="padding:1rem;color:var(--danger);">加载日志失败: ${e.message}</p>`;
    }
  },
};

document.addEventListener('DOMContentLoaded', () => App.init());
