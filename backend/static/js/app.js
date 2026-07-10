const App = {
  token: localStorage.getItem('token') || '',
  streamModule: null,
  streamInterval: null,
  wsAlerts: null,
  wsStream: null,
  assistantHistory: [],
  assistantThinking: false,
  assistantRecognition: null,
  assistantVoiceEnabled: true,

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
