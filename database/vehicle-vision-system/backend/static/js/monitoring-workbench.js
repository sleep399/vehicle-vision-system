/* 完整日志监控、告警工作台与悬浮 Alert Agent：从组员版本按功能隔离移植。 */
(() => {
  Object.assign(App, {
  // Anthropic 设计 Token 色值 (用于 JS 内联样式)
  _colors: {
    error: '#C0453A',
    warning: '#C9943A',
    success: '#6B8F47',
    info: '#5A89B8',
    accent: '#D97757',
    muted: '#9B9890',
  },
  SEVERITY_COLORS: {
    critical: '#C0453A',
    warning: '#C9943A',
    info: '#5A89B8',
  },
  token: localStorage.getItem('token') || '',
  wsAlerts: null,
  sseSource: null,
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
  agentDragMoved: false,
  agentSpeechTimer: null,
  _agentPointerId: null,
  agentMonitorTimer: null,
  agentBriefing: null,
  agentLastBriefKey: '',
  _lastAgentDrivingAdviceKey: '',
  focusedAlert: null,
  currentView: '',
  logSseSource: null,
  recognitionMirrors: {
    lpr: { status: 'idle', statusText: '未运行', source: '车牌识别模块', previewSrc: '', result: null, updatedAt: null },
    police: { status: 'idle', statusText: '未运行', source: '交警手势模块', previewSrc: '', result: null, updatedAt: null },
    owner: { status: 'idle', statusText: '未运行', source: '车主控车模块', previewSrc: '', result: null, vehicleState: null, updatedAt: null },
  },
  scenarioFusionRefresh: {
    timer: null,
    queuedAt: 0,
    inFlight: false,
    dirty: false,
  },
  ownerVehicleRefreshTimer: null,
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
  AGENT_EVENT_LABELS: {
    lpr_consecutive_failure: '车牌识别连续失败',
    lpr_high_failure_rate: '车牌识别失败率过高',
    gesture_low_confidence: '手势识别置信度持续偏低',
    llm_api_timeout: 'LLM API 调用超时',
    llm_token_exhausted: 'LLM Token 配额即将耗尽',
    llm_token_exceeded: 'LLM Token 配额已超额',
    unauthorized_access: '未授权访问尝试',
    service_unhealthy: '系统服务健康异常',
    model_load_failure: 'AI 模型加载失败',
    database_connection_error: '数据库连接异常',
    webhook_delivery_failure: 'Webhook 推送失败',
    email_delivery_failure: '邮件推送失败',
    config_missing: '关键配置缺失',
    test_event: '测试告警',
    scenario_conflict_detected: '多路感知场景冲突',
    fusion_recommendation_issued: '融合处置建议已下发',
    owner_action_suppressed: '车主控车动作已抑制',
  },
  AGENT_LEVEL_LABELS: { info: '提示', warning: '警告', critical: '严重' },
  ALERT_LEVEL_LABELS: { info: '提示', warning: '警告', critical: '严重' },
  AGENT_CHANNEL_LABELS: { web: '网页', sse: 'SSE推送', webhook: 'Webhook', email: '邮件' },
  RECORD_TYPE_LABELS: {
    lpr: '车牌识别',
    police_gesture: '交警手势',
    owner_gesture: '车主手势',
    record: '识别记录',
  },

  humanizeLogDisplay(log) {
    if (!log) return '';
    if (log.display_message) return log.display_message;
    return this.sanitizeLogMessageText(log.message, log.detail_json || log.detail);
  },

  humanizeErrorText(text) {
    let result = String(text || '').trim();
    if (!result) return result;
    const replacements = [
      [/The following operation failed in the TorchScript interpreter\.?/gi,
        'AI 模型推理失败，可能是模型文件损坏或与当前运行环境不兼容'],
      [/Expected all tensors to be on the same device.*/gi,
        '模型计算设备不一致（CPU/GPU 混用），请重启服务或检查模型加载配置'],
      [/CUDA out of memory.*/gi, '显卡显存不足，请减小图片尺寸或关闭其他占用显存的程序'],
      [/CUDA error:? .*/gi, '显卡运行异常，请检查 CUDA 驱动或改用 CPU 模式'],
      [/No module named ['"]([^'"]+)['"]/gi, '缺少依赖模块「$1」，请联系管理员安装'],
      [/Connection refused/gi, '连接被拒绝，目标服务可能未启动'],
      [/Connection reset by peer/gi, '连接被远端重置，请检查网络或服务状态'],
      [/timed out|TimeoutError|timeout/gi, '请求超时，请稍后重试'],
      [/Unable to open RTSP stream|无法打开 RTSP 流/gi, '无法打开 RTSP 视频流，请检查地址与网络'],
      [/Failed to load model|Error loading model/gi, '模型加载失败，请检查模型文件是否完整'],
      [/LLM API Key 未配置|LLM API key not configured/gi, '智能分析服务密钥未配置'],
      [/401 Unauthorized|403 Forbidden|404 Not Found|500 Internal Server Error/gi, '服务请求失败，请稍后重试'],
      [/Network is unreachable/gi, '网络不可达，请检查网络连接'],
      [/Permission denied/gi, '权限不足，无法访问目标资源'],
      [/Address already in use/gi, '端口已被占用，请更换端口或关闭冲突进程'],
    ];
    replacements.forEach(([pattern, replacement]) => {
      result = result.replace(pattern, replacement);
    });
    const excMatch = result.match(/^([A-Za-z_][\w.]*(?:Error|Exception)):\s*(.+)$/s);
    if (excMatch) {
      const typeMap = {
        RuntimeError: '运行错误', ValueError: '参数错误', TypeError: '类型错误',
        FileNotFoundError: '文件未找到', ConnectionError: '连接错误', TimeoutError: '请求超时',
        ImportError: '依赖缺失', ModuleNotFoundError: '模块未找到',
      };
      const typeCn = typeMap[excMatch[1]] || '系统异常';
      const inner = this.humanizeErrorText(excMatch[2].trim());
      return `${typeCn}：${inner || '运行出现异常，请稍后重试'}`;
    }
    const hasChinese = /[\u4e00-\u9fff]/.test(result);
    const latinRatio = (result.match(/[a-z]/gi) || []).length / Math.max(result.length, 1);
    const technical = /error|exception|traceback|failed|interpreter|torchscript|cuda|runtime|module|tensor/i.test(result);
    if (!hasChinese && (technical || (latinRatio > 0.55 && result.length > 12))) {
      return '系统运行异常，请稍后重试或联系管理员';
    }
    return result;
  },

  sanitizeLogMessageText(message, detail) {
    let msg = String(message || '').trim();
    const d = detail && typeof detail === 'object' ? detail : null;
    if (d && (d.error_message || d.error)) {
      const err = this.humanizeErrorText(d.error_message || d.error);
      if (msg.includes('Traceback') || msg.length > 160 || msg.includes('File "')) {
        const head = msg.split('Traceback (most recent call last)')[0].trim().replace(/[:：]$/, '');
        return `${this.humanizeErrorText(head || '系统运行异常')}：${err}`;
      }
      if (err && !msg.includes(err)) return `${this.humanizeErrorText(msg)}（${err}）`;
    }
    if (msg.includes('Traceback (most recent call last)')) {
      const head = msg.split('Traceback (most recent call last)')[0].trim();
      return this.humanizeErrorText(head || '系统运行出现异常，请稍后重试');
    }
    if (msg.length > 220 && (msg.includes('\\') || msg.includes('site-packages'))) {
      const line = msg.split('\n').find(l => l.trim() && !l.includes('File "') && !l.includes('site-packages'))?.trim()?.slice(0, 200);
      return this.humanizeErrorText(line || msg.slice(0, 120) + '…');
    }
    return this.humanizeErrorText(msg) || '（无详细说明）';
  },

  recordTypeLabel(type) {
    return this.RECORD_TYPE_LABELS[type] || this.logCategoryLabel(type) || type || '识别记录';
  },

  alertStatusLabel(status) {
    return status === 'resolved' ? '已处理' : (status === 'open' ? '未处理' : (status || '未知'));
  },

  /** 构建助手请求：携带显式告警上下文与近期对话历史 */
  buildAssistantPayload(question, intent = null) {
    const body = { question };
    if (intent) body.intent = intent;
    const alertId = this.getExplicitAlertId();
    if (alertId) body.alert_id = alertId;
    const history = this.assistantHistory.slice(-10).map(msg => ({
      role: msg.role,
      content: msg.content,
    }));
    if (history.length) body.history = history;
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
   * 快捷提问（根因/建议/升级/影响）—— 显式传 intent，避免 LLM 四答雷同
   */
  askAboutAlert(question, intent) {
    this.askAssistant(question, intent);
  },

  /** 用户能听懂的简短告警话术 */
  alertToUserSpeech(alert) {
    const title = alert.title || '系统提醒';
    const summary = alert.summary || '';
    if (summary && summary.length < 60) return summary;
    return title;
  },


  // ── WebSocket & SSE ──
  monitorStreamUrl(path) {
    const url = new URL(this.apiUrl(path));
    if (this.token) url.searchParams.set('token', this.token);
    return url.toString();
  },

  connectAlertWs() {
    if (this.wsAlerts && this.wsAlerts.readyState === WebSocket.OPEN) return;
    const tokenQuery = this.token ? `?token=${encodeURIComponent(this.token)}` : '';
    this.wsAlerts = new WebSocket(`${this.wsBase()}/ws/alerts${tokenQuery}`);
    this.wsAlerts.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'alert') {
        this.showToast(data);
        this.prependLiveAlert(data);
        this.onAgentAlert(data);
      }
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
    this.sseSource = new EventSource(this.monitorStreamUrl('/api/monitor/stream'));
    this.sseSource.onopen = () => {
      document.getElementById('stat-sse-conn') && (document.getElementById('stat-sse-conn').textContent = '1');
    };
    this.sseSource.addEventListener('connected', (e) => {
      console.log('SSE connected:', JSON.parse(e.data));
    });
    this.sseSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'alert') {
          this.showToast(data);
          this.prependLiveAlert(data);
          this.onAgentAlert(data);
        }
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

  showToast(alert) {
    const el = document.createElement('div');
    el.className = 'toast ' + (alert.level || '');
    el.innerHTML = this.formatAlertToast(alert);
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.3s'; setTimeout(() => el.remove(), 300); }, 5000);
  },

  formatAlertToast(alert) {
    const levelText = alert.level_cn || this.agentLevelLabel(alert.level);
    const typeText = alert.event_type_cn || this.agentEventLabel(alert.event_type);
    return `<div class="toast-header">
      ${this.agentLogChip('level', levelText, alert.level)}
      ${typeText ? this.agentLogChip('type', typeText) : ''}
      <strong class="toast-title">${this.escHtml(alert.title || '系统提醒')}</strong>
    </div>
    <small class="toast-summary">${this.escHtml(alert.summary || '')}</small>`;
  },

  prependLiveAlert(alert) {
    const container = document.getElementById('live-alerts');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'alert-item ' + (alert.level || '');
    const time = alert.created_at ? new Date(alert.created_at).toLocaleString() : new Date().toLocaleString();
    div.innerHTML = `
      <div class="alert-title">${this.escHtml(alert.title)}</div>
      <div class="alert-summary">${this.escHtml(alert.summary || '')}</div>
      <div class="alert-meta">${time} · ${this.escHtml(alert.event_type_cn || this.agentEventLabel(alert.event_type) || '告警')} · ${this.escHtml(this.agentChannelsText((alert.channels || alert.channels_sent || 'web').split(',')))}</div>
      ${alert.suggestion ? `<div class="alert-suggestion">💡 ${this.escHtml(alert.suggestion)}</div>` : ''}
    `;
    container.prepend(div);
  },

  _alertFilterParams() {
    const level = (document.getElementById('alert-filter-level') && document.getElementById('alert-filter-level').value) || '';
    const eventType = (document.getElementById('alert-filter-type') && document.getElementById('alert-filter-type').value) || '';
    const status = (document.getElementById('alert-filter-status') && document.getElementById('alert-filter-status').value) || '';
    const startEl = document.getElementById('alert-filter-start');
    const endEl = document.getElementById('alert-filter-end');
    const start = startEl && startEl.value ? startEl.value + 'T00:00:00' : '';
    const end = endEl && endEl.value ? endEl.value + 'T23:59:59' : '';
    let qs = '';
    if (level) qs += '&level=' + encodeURIComponent(level);
    if (eventType) qs += '&event_type=' + encodeURIComponent(eventType);
    if (status) qs += '&status=' + encodeURIComponent(status);
    if (start) qs += '&start=' + encodeURIComponent(start);
    if (end) qs += '&end=' + encodeURIComponent(end);
    return qs;
  },

  _alertFilterSummary() {
    const parts = [];
    const levelEl = document.getElementById('alert-filter-level');
    const typeEl = document.getElementById('alert-filter-type');
    const statusEl = document.getElementById('alert-filter-status');
    const levelLabels = { info: '提示', warning: '警告', critical: '严重' };
    if (levelEl && levelEl.value) parts.push(`级别：${levelLabels[levelEl.value] || levelEl.value}`);
    if (typeEl && typeEl.value) {
      const text = typeEl.selectedOptions[0]?.text || typeEl.value;
      parts.push(`类型：${text.replace(/\s*\([^)]*\)\s*$/, '')}`);
    }
    if (statusEl && statusEl.value) {
      parts.push(`状态：${statusEl.value === 'open' ? '未处理' : '已处理'}`);
    }
    return parts.length ? `筛选：${parts.join(' · ')} · ` : '';
  },

  async onAlertFilterChange() {
    await this.resetAlertTimeline();
    const panel = document.getElementById('alert-timeline');
    if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  },

  async refreshAlerts() {
    const btn = document.getElementById('alert-refresh-btn');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '刷新中…';
    }
    try {
      await this.resetAlertTimeline();
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '刷新';
      }
    }
  },

  renderAlertStructured(a) {
    const impact = a.impact_scope || (a.detail && a.detail.structured && a.detail.structured.impact_scope);
    const occurred = a.occurred_at || (a.detail && a.detail.structured && a.detail.structured.occurred_at);
    const sev = a.severity_assessment || (a.detail && a.detail.structured && a.detail.structured.severity_assessment);
    if (!a.root_cause && !a.suggestion && !impact && !sev) return '';
    return `
      <div class="alert-structured-grid">
        ${occurred ? `<div class="alert-struct-item"><span class="alert-struct-label">发生时间</span><span>${this.escHtml(occurred)}</span></div>` : ''}
        ${impact ? `<div class="alert-struct-item"><span class="alert-struct-label">影响范围</span><span>${this.escHtml(impact)}</span></div>` : ''}
        ${a.root_cause ? `<div class="alert-struct-item"><span class="alert-struct-label">根因</span><span>${this.escHtml(a.root_cause)}</span></div>` : ''}
        ${a.suggestion ? `<div class="alert-struct-item"><span class="alert-struct-label">建议处置</span><span>${this.escHtml(a.suggestion)}</span></div>` : ''}
        ${sev && sev.decision_reason ? `<div class="alert-struct-item"><span class="alert-struct-label">级别决策</span><span>${this.escHtml(sev.decision_reason)}</span></div>` : ''}
      </div>`;
  },

  renderTimelineItem(a) {
    const statusBadge = a.status === 'resolved'
      ? '<span class="badge resolved">已处理</span>'
      : '<span class="badge open">未处理</span>';
    const channelsText = this.agentChannelsText(a.channels || a.channels_sent || 'web');
    return `
      <div class="timeline-item severity-${this.severityKey(a.level)} ${a.level}">
        <div class="timeline-content">
          <div class="timeline-header">
            <span>${this.renderLevelPill(a.level, this.ALERT_LEVEL_LABELS[a.level] || this.agentLevelLabel(a.level))} <strong>${this.escHtml(a.title)}</strong> ${statusBadge}</span>
            <div class="timeline-actions">
              <button class="btn small" onclick="App.viewReplay(${a.id})" title="事件回放">▶ 回放</button>
              <button class="btn small" onclick="App.showCauseAnalysis(${a.id})" title="根因分析">🔍 根因</button>
              ${a.status !== 'resolved' ? `<button class="btn small primary" onclick="App.resolveAlert(${a.id})">✓ 处理</button>` : ''}
            </div>
          </div>
          <p class="timeline-summary">${this.escHtml(a.summary || '')}</p>
          ${this.renderAlertStructured(a)}
          <div class="timeline-meta">
            <span>🕐 ${new Date(a.created_at).toLocaleString()}</span>
            <span>📌 ${this.escHtml(a.event_type_cn || a.event_type)}</span>
            <span>📡 ${this.escHtml(channelsText)}</span>
          </div>
          ${a.root_cause ? `<div class="timeline-cause hidden-legacy">🔍 根因：${this.escHtml(a.root_cause)}</div>` : ''}
          ${a.suggestion ? `<div class="timeline-suggestion hidden-legacy">💡 建议：${this.escHtml(a.suggestion)}</div>` : ''}
        </div>
      </div>`;
  },

  renderTimelineGroups(groups, append = false) {
    const el = document.getElementById('alert-timeline');
    if (!el) return;
    const html = (groups || []).map(g => `
      <div class="timeline-date-group">
        <div class="timeline-date-header">${g.date}</div>
        ${g.items.map(a => this.renderTimelineItem(a)).join('')}
      </div>
    `).join('');
    if (append) {
      el.innerHTML += html;
    } else {
      el.innerHTML = html || '<p style="color:var(--text-muted);padding:1rem;">暂无告警记录</p>';
    }
  },

  async loadAlerts(append = false) {
    try {
      if (!append) this.alertTimelineSkip = 0;
      const qs = this._alertFilterParams();
      const skip = this.alertTimelineSkip;

      const [stats, timeline] = await Promise.all([
        this.api('/api/monitor/alerts/stats'),
        this.api(`/api/monitor/alerts/timeline?limit=30&skip=${skip}${qs}`),
      ]);

      const statsEl = document.getElementById('alert-stats');
      statsEl.innerHTML = `
        <div class="stat-card"><div class="stat-num">${stats.total}</div><div class="stat-label">总计</div></div>
        <div class="stat-card"><div class="stat-num">${stats.open || 0}</div><div class="stat-label">未处理</div></div>
        <div class="stat-card"><div class="stat-num">${stats.open_critical || 0}</div><div class="stat-label">严重未处理</div></div>
        <div class="stat-card"><div class="stat-num">${stats.today_count || 0}</div><div class="stat-label">今日新增</div></div>
        <div class="stat-card"><div class="stat-num">${stats.resolution_rate || 0}%</div><div class="stat-label">处理率</div></div>
        <div class="stat-card"><div class="stat-num small">${stats.mttr_minutes != null ? stats.mttr_minutes + '分' : '-'}</div><div class="stat-label">平均处理时长</div></div>
      `;

      this.renderDistribution(stats);
      this.renderTimelineGroups(timeline.groups, append);

      this.alertTimelineHasMore = timeline.has_more;
      this.alertTimelineSkip = skip + (timeline.groups || []).reduce((n, g) => n + g.items.length, 0);

      const infoEl = document.getElementById('alert-timeline-info');
      const moreBtn = document.getElementById('alert-load-more');
      if (infoEl) {
        infoEl.textContent = `${this._alertFilterSummary()}共 ${timeline.total} 条，已加载 ${Math.min(this.alertTimelineSkip, timeline.total)} 条`;
      }
      if (moreBtn) moreBtn.style.display = timeline.has_more ? 'inline-block' : 'none';

    } catch (e) {
      console.error('Load alerts error:', e);
      this.showToast({ level: 'critical', title: '加载告警失败', summary: e.message || '请检查网络或稍后重试' });
    }
  },

  async resetAlertTimeline() {
    this.alertTimelineSkip = 0;
    await this.loadAlerts(false);
  },

  loadMoreAlerts() {
    if (this.alertTimelineHasMore) this.loadAlerts(true);
  },

  async loadAlertAnalytics() {
    try {
      const daysEl = document.getElementById('alert-analytics-days');
      const days = daysEl ? daysEl.value : 7;
      const data = await this.api(`/api/monitor/alerts/analytics?days=${days}`);
      this.renderAlertAnalytics(data, 'alert-analytics', false);
    } catch (e) { console.error('Load analytics error:', e); }
  },

  renderAlertAnalytics(data, containerId, compact) {
    const el = document.getElementById(containerId);
    if (!el || !data) return;

    const byLevel = data.by_level || {};
    const maxLevel = Math.max(...Object.values(byLevel), 1);
    const levelColors = this.SEVERITY_COLORS;
    const levelLabels = this.ALERT_LEVEL_LABELS;

    let levelHtml = '<div class="analytics-section"><h4>级别分布</h4><div class="dist-bars">';
    for (const [level, count] of Object.entries(byLevel)) {
      const pct = Math.round(count / maxLevel * 100);
      const key = this.severityKey(level);
      levelHtml += `<div class="dist-row"><span class="dist-label">${levelLabels[level] || this.logLevelLabel(level)}</span>
        <div class="dist-bar-track"><div class="dist-bar-fill severity-bar-${key}" style="width:${pct}%;background:${levelColors[key] || this._colors.muted};"></div></div>
        <span class="dist-count">${count}</span></div>`;
    }
    levelHtml += '</div></div>';

    const ranked = data.by_type_ranked || [];
    const maxType = Math.max(...ranked.map(t => t.count), 1);
    let typeHtml = '<div class="analytics-section"><h4>类型 TOP</h4><div class="dist-bars">';
    for (const t of ranked.slice(0, compact ? 5 : 8)) {
      const pct = Math.round(t.count / maxType * 100);
      typeHtml += `<div class="dist-row"><span class="dist-label dist-label-wide" title="${this.escHtml(t.name)}">${this.escHtml(t.name)}</span>
        <div class="dist-bar-track"><div class="dist-bar-fill" style="width:${pct}%;background:var(--color-accent-orange);"></div></div>
        <span class="dist-count">${t.count}</span></div>`;
    }
    typeHtml += '</div></div>';

    const hourly = data.hourly_distribution || [];
    const maxHour = Math.max(...hourly.map(h => h.count), 1);
    let hourHtml = '<div class="analytics-section"><h4>24 小时分布</h4><div class="hourly-chart">';
    for (const h of hourly) {
      const hPct = Math.max(4, Math.round(h.count / maxHour * 100));
      hourHtml += `<div class="hourly-bar" title="${h.label}: ${h.count}条">
        <div class="hourly-fill" style="height:${hPct}%;"></div>
        <span class="hourly-label">${h.hour % 6 === 0 ? h.label : ''}</span>
      </div>`;
    }
    hourHtml += '</div></div>';

    const trends = data.date_trend || [];
    const maxTrend = Math.max(...trends.map(t => t.count), 1);
    let trendHtml = '<div class="analytics-section"><h4>日期趋势</h4><div class="dist-bars">';
    for (const t of trends.slice(compact ? -7 : -14)) {
      const pct = Math.round(t.count / maxTrend * 100);
      trendHtml += `<div class="dist-row"><span class="dist-label">${t.date.slice(5)}</span>
        <div class="dist-bar-track"><div class="dist-bar-fill" style="width:${pct}%;background:var(--color-info);"></div></div>
        <span class="dist-count">${t.count}</span></div>`;
    }
    trendHtml += '</div></div>';

    const kpiHtml = compact ? '' : `
      <div class="analytics-kpi">
        <div class="analytics-kpi-item"><span class="kpi-num">${data.total || 0}</span><span class="kpi-label">区间总数</span></div>
        <div class="analytics-kpi-item"><span class="kpi-num">${data.resolution_rate || 0}%</span><span class="kpi-label">处理率</span></div>
        <div class="analytics-kpi-item"><span class="kpi-num">${data.mttr_minutes != null ? data.mttr_minutes + '分' : '-'}</span><span class="kpi-label">MTTR</span></div>
        <div class="analytics-kpi-item"><span class="kpi-num">${data.open || 0}</span><span class="kpi-label">未处理</span></div>
      </div>`;

    el.innerHTML = kpiHtml + `<div class="analytics-grid${compact ? ' compact' : ''}">` + levelHtml + typeHtml + hourHtml + trendHtml + '</div>';
  },

  renderCauseAnalysisHtml(cause) {
    if (!cause) return '<p style="color:var(--text-muted);">暂无根因分析数据</p>';
    const chain = (cause.cause_chain || []).map(c => {
      let titleText = c.title || '';
      if (c.category) {
        titleText = `${this.logCategoryLabel(c.category)} · ${this.logLevelLabel(c.level)}`;
      } else if (titleText.includes(' · ')) {
        const [cat, lv] = titleText.split(' · ');
        titleText = `${this.logCategoryLabel(cat.trim())} · ${this.logLevelLabel(lv.trim())}`;
      }
      let desc = c.description || '';
      if (c.type === 'log' || c.type === 'alert') {
        desc = this.humanizeAgentLogMessage(desc);
      }
      return `
      <div class="cause-chain-item cause-${c.type}">
        <div class="cause-chain-step">${c.step}</div>
        <div class="cause-chain-body">
          <strong>${this.escHtml(titleText)}</strong>
          <p>${this.escHtml(desc)}</p>
          ${c.timestamp ? `<span class="cause-time">${new Date(c.timestamp).toLocaleString()}</span>` : ''}
        </div>
      </div>`;
    }).join('');

    const factors = (cause.contributing_factors || []).map(f =>
      `<li>${this.escHtml(f)}</li>`
    ).join('');

    return `
      <div class="cause-analysis-panel">
        <h5>🔍 根因分析</h5>
        <div class="cause-primary">${this.escHtml(cause.primary_cause)}</div>
        ${cause.impact ? `<div class="cause-impact">⚡ 影响评估：${this.escHtml(cause.impact)}</div>` : ''}
        ${factors ? `<div class="cause-factors"><strong>关联因素</strong><ul>${factors}</ul></div>` : ''}
        ${cause.suggestion ? `<div class="cause-suggestion">💡 ${this.escHtml(cause.suggestion)}</div>` : ''}
        ${chain ? `<div class="cause-chain"><strong>因果链</strong>${chain}</div>` : ''}
        <button class="btn small" onclick="App.askCauseDeep()">🤖 AI 深度分析</button>
      </div>`;
  },

  async showCauseAnalysis(alertId) {
    await this.viewReplay(alertId);
    const panel = document.querySelector('.cause-analysis-panel');
    if (panel) panel.scrollIntoView({ behavior: 'smooth' });
  },

  askCauseDeep() {
    if (!this.getExplicitAlertId()) {
      this.addAssistantMessage('user', '请对这个告警进行深度根因分析，说明因果链、影响范围和推荐处置步骤。');
      this.addAssistantMessage('assistant',
        '您指的是哪条告警？请先在告警回放页打开某条告警，再点「AI 深度分析」。');
      return;
    }
    this.askAboutAlert('请对这个告警进行深度根因分析，说明因果链、影响范围和推荐处置步骤。');
  },

  renderDistribution(stats) {
    const el = document.getElementById('alert-distribution');
    if (!el) return;
    const byLevel = stats.by_level || {};
    const maxVal = Math.max(...Object.values(byLevel), 1);

    let html = '<div class="dist-bars">';
    for (const [level, count] of Object.entries(byLevel)) {
      const pct = Math.round(count / maxVal * 100);
      const color = this.severityColor(level);
      html += `
        <div class="dist-row">
          <span class="dist-label">${this.ALERT_LEVEL_LABELS[level] || this.logLevelLabel(level)}</span>
          <div class="dist-bar-track"><div class="dist-bar-fill severity-bar-${this.severityKey(level)}" style="width:${pct}%;background:${color};"></div></div>
          <span class="dist-count">${count}</span>
        </div>`;
    }
    html += '</div>';

    // Date trend
    const trends = stats.date_trend || [];
    if (trends.length > 0) {
      html += '<h4 style="margin-top:1rem;margin-bottom:.5rem;">每日趋势</h4><div class="dist-bars">';
      const maxTrend = Math.max(...trends.map(t => t.count), 1);
      for (const t of trends.slice(-14)) {
        const pct = Math.round(t.count / maxTrend * 100);
        html += `
          <div class="dist-row">
            <span class="dist-label">${t.date.slice(5)}</span>
            <div class="dist-bar-track"><div class="dist-bar-fill" style="width:${pct}%;background:var(--color-accent-orange);"></div></div>
            <span class="dist-count">${t.count}</span>
          </div>`;
      }
      html += '</div>';
    }
    el.innerHTML = html;
  },

  async viewReplay(alertId) {
    this.currentReplayId = alertId;
    this.replayStopPlay();
    try {
      const data = await this.api(`/api/monitor/alerts/${alertId}/replay`);
      const panel = document.getElementById('replay-panel');
      panel.style.display = 'block';
      const a = data.alert;
      this.setFocusedAlert(a);
      const causeHtml = this.renderCauseAnalysisHtml(data.cause_analysis);

      panel.querySelector('#replay-content').innerHTML = `
        <div class="replay-alert ${a.level}">
          <h4>${this.escHtml(a.title)}</h4>
          <table class="replay-table">
            <tr><td class="replay-key">级别</td><td><span class="badge ${a.level}">${this.escHtml(a.level_cn || this.agentLevelLabel(a.level))}</span></td></tr>
            <tr><td class="replay-key">类型</td><td>${this.escHtml(a.event_type_cn || a.event_type)}</td></tr>
            <tr><td class="replay-key">时间</td><td>${new Date(a.created_at).toLocaleString()}</td></tr>
            <tr><td class="replay-key">状态</td><td>${this.escHtml(a.status_cn || (a.status === 'resolved' ? '已处理' : '未处理'))}</td></tr>
            <tr><td class="replay-key">推送渠道</td><td>${this.escHtml(this.agentChannelsText(a.channels || 'web'))}</td></tr>
          </table>
          <h5>📋 摘要</h5><p>${this.escHtml(a.summary || '无')}</p>
          ${this.renderAlertStructured(a)}
          ${causeHtml}
          ${a.resolution_note ? `<h5>✅ 处理说明</h5><p>${this.escHtml(a.resolution_note)}</p>` : ''}
        </div>
        ${(data.related_records && data.related_records.length) ? `
        <h5 style="margin-top:1rem;">🖼️ 关联识别记录 (${data.related_records.length}条)</h5>
        <div class="replay-records">${data.related_records.map(r =>
          `<div class="replay-record">
            <span>${this.escHtml(r.type_cn || this.recordTypeLabel(r.type))} #${r.id} · ${r.created_at ? new Date(r.created_at).toLocaleString() : ''}</span>
            ${r.gesture_cn ? `<span>${this.escHtml(r.gesture_cn)}（置信度 ${Math.round((r.confidence || 0) * 100)}%）</span>` : ''}
            ${r.annotated_image ? `<img src="${r.annotated_image}" alt="识别结果" style="max-width:100%;margin-top:0.5rem;border-radius:8px;">` : ''}
          </div>`
        ).join('')}</div>` : ''}
        <h5 style="margin-top:1rem;">📄 关联日志 (${(data.related_logs && data.related_logs.length) || 0}条)</h5>
        <div class="replay-logs">${(data.related_logs || []).map(l =>
          `<div class="log-row severity-${this.severityKey(l.level_cn || l.level)}">
            <span>${new Date(l.created_at).toLocaleString()}</span>
            <span>${this.renderLevelPill(l.level_cn || l.level, this.logLevelLabel(l.level_cn || l.level))}</span>
            <span>${this.escHtml(l.category_cn || this.logCategoryLabel(l.category))}</span>
            <span>${this.escHtml(this.humanizeLogDisplay(l))}</span>
          </div>`
        ).join('') || '<p style="color:var(--text-muted);">无关联日志</p>'}</div>
      `;

      this.replayEvents = data.timeline_events || [];
      this.replayStepIndex = 0;
      const playerEl = document.getElementById('replay-player');
      if (playerEl) {
        playerEl.style.display = this.replayEvents.length > 0 ? 'block' : 'none';
        this.renderReplayStep();
      }

      panel.scrollIntoView({ behavior: 'smooth' });
    } catch (e) { alert('获取回放数据失败: ' + e.message); }
  },

  renderReplayStep() {
    const view = document.getElementById('replay-step-view');
    const info = document.getElementById('replay-step-info');
    if (!view || !this.replayEvents.length) return;

    const idx = this.replayStepIndex;
    const ev = this.replayEvents[idx];
    const typeIcon = { log: '📄', record: '🖼️', health: '🖥️', alert: '🚨' };
    const levelClass = this.severityKey(ev.level);

    view.innerHTML = `
      <div class="replay-step-card ${levelClass}">
        <div class="replay-step-header">
          <span>${typeIcon[ev.type] || '•'} ${this.escHtml(ev.title)}</span>
          <span class="replay-step-time">${ev.time ? new Date(ev.time).toLocaleString() : ''}</span>
        </div>
        ${ev.image ? `<img src="${ev.image}" alt="回放截图" class="replay-step-img">` : ''}
      </div>`;

    if (info) info.textContent = `${idx + 1} / ${this.replayEvents.length}`;
  },

  replayStep(delta) {
    if (!this.replayEvents.length) return;
    this.replayStepIndex = Math.max(0, Math.min(this.replayEvents.length - 1, this.replayStepIndex + delta));
    this.renderReplayStep();
  },

  replayTogglePlay() {
    if (this.replayPlayTimer) {
      this.replayStopPlay();
      return;
    }
    const btn = document.getElementById('replay-play-btn');
    if (btn) btn.textContent = '⏸ 暂停';
    this.replayPlayTimer = setInterval(() => {
      if (this.replayStepIndex >= this.replayEvents.length - 1) {
        this.replayStopPlay();
        return;
      }
      this.replayStep(1);
    }, 1500);
  },

  replayStopPlay() {
    if (this.replayPlayTimer) {
      clearInterval(this.replayPlayTimer);
      this.replayPlayTimer = null;
    }
    const btn = document.getElementById('replay-play-btn');
    if (btn) btn.textContent = '▶ 播放';
  },

  closeReplay() {
    this.replayStopPlay();
    document.getElementById('replay-panel').style.display = 'none';
    this.currentReplayId = null;
    this.replayEvents = [];
    this.replayStepIndex = 0;
  },

  async resolveAlert(alertId) {
    try {
      await this.api(`/api/monitor/alerts/${alertId}/resolve`, { method: 'POST', body: JSON.stringify({ resolution_note: '手动处理' }) });
      if (this.focusedAlert && this.focusedAlert.id === alertId) this.clearFocusedAlert();
      if (this.currentReplayId === alertId) this.closeReplay();
      this.loadAlerts();
      this.loadAlertAnalytics();
    } catch (e) { alert(e.message); }
  },

  async testAlert() {
    try {
      const data = await this.api('/api/monitor/alerts/test', { method: 'POST' });
      this.showToast({ level: data.level, title: data.title, summary: data.summary });
      this.loadAlerts();
      this.loadAlertAnalytics();
    } catch (e) { alert(e.message); }
  },

  async loadAlertTypes() {
    try {
      const types = await this.api('/api/monitor/alerts/event-types');
      this.alertTypes = types;
      const filterSelect = document.getElementById('alert-filter-type');
      const testSelect = document.getElementById('test-alert-type');
      const levelLabels = { info: '提示', warning: '警告', critical: '严重' };
      const options = types.map(t => {
        const lv = t.default_level_cn || levelLabels[t.default_level] || t.default_level;
        return `<option value="${t.key}">${t.name} (${lv})</option>`;
      }).join('');
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
        const levelLabel = this.logLevelLabel(log.level_cn || log.level);
        const severity = this.severityKey(log.level_cn || log.level);
        return `<div class="agent-activity-item severity-${severity}">
          <span class="agent-activity-time">${time}</span>
          <span class="agent-activity-level">${this.renderLevelPill(log.level, levelLabel)}</span>
          <div class="agent-activity-msg">${this.formatAgentLogDisplay(log)}</div>
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
    const ids = ['log-category', 'log-level', 'log-search', 'log-start', 'log-end'];
    const defaults = { 'log-category': '', 'log-level': '', 'log-search': '', 'log-start': '', 'log-end': '' };
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

  agentEventLabel(key) {
    if (!key) return '';
    return this.AGENT_EVENT_LABELS[key] || (this.alertTypes.find(t => t.key === key) || {}).name || key;
  },

  agentLevelLabel(level) {
    if (!level) return '';
    const text = String(level).trim().toLowerCase();
    return this.AGENT_LEVEL_LABELS[text] || level;
  },

  agentChannelLabel(channel) {
    if (!channel) return '';
    const key = String(channel).trim().replace(/['"]/g, '');
    return this.AGENT_CHANNEL_LABELS[key] || key;
  },

  agentChannelsText(channels) {
    if (!channels) return '';
    if (typeof channels === 'string') {
      if (channels.includes(',') && !channels.startsWith('[')) {
        return channels.split(',').map(c => this.agentChannelLabel(c.trim())).filter(Boolean).join(' · ');
      }
      const match = channels.match(/\[([^\]]+)\]/);
      if (match) {
        return match[1].split(',').map(c => this.agentChannelLabel(c)).filter(Boolean).join(' · ');
      }
      if (channels.includes('、') || channels.includes('·')) return channels;
    }
    const list = Array.isArray(channels) ? channels : [channels];
    return list.map(c => this.agentChannelLabel(c)).filter(Boolean).join(' · ');
  },

  agentLogChip(kind, text, level) {
    if (!text) return '';
    const severity = kind === 'level' ? this.severityKey(level) : '';
    const levelClass = severity ? ` severity-${severity}` : '';
    return `<span class="agent-log-chip ${kind}${levelClass}">${this.escHtml(text)}</span>`;
  },

  formatAgentLogDisplay(log) {
    const msg = log.message || '';
    const detail = log.detail_json || log.detail || {};
    const pushMatch = msg.match(/告警\s*#(\d+)\s*已推送/);
    if (detail.alert_id || pushMatch) {
      const alertId = detail.alert_id || pushMatch[1];
      let eventType = detail.event_type;
      if (!eventType) {
        Object.keys(this.AGENT_EVENT_LABELS).some(key => {
          if (msg.includes(key)) { eventType = key; return true; }
          return false;
        });
      }
      const typeText = detail.event_type_cn || this.agentEventLabel(eventType);
      const levelKey = detail.level || (msg.match(/\[(critical|warning|info)\]/i) || [])[1];
      const levelText = detail.level_cn || this.agentLevelLabel(levelKey);
      const channelsText = detail.channels_cn || this.agentChannelsText(detail.channels || msg);
      return `<div class="agent-log-line">
        <strong class="agent-log-action">告警 #${this.escHtml(String(alertId))} 已推送</strong>
        ${this.agentLogChip('type', typeText)}
        ${this.agentLogChip('level', levelText, levelKey || levelText)}
        ${this.agentLogChip('channel', channelsText)}
      </div>`;
    }
    const decisionMatch = msg.match(/告警级别决策:\s*(.+?)\s*→\s*(.+)$/);
    if (decisionMatch || detail.decided_level) {
      const typeText = detail.event_type_cn || this.agentEventLabel(detail.event_type) || decisionMatch?.[1]?.trim();
      const levelText = detail.decided_level_cn || this.agentLevelLabel(detail.decided_level) || decisionMatch?.[2]?.trim();
      return `<div class="agent-log-line">
        <strong class="agent-log-action">级别决策</strong>
        ${this.agentLogChip('type', typeText)}
        <span class="agent-log-arrow">→</span>
        ${this.agentLogChip('level', levelText, detail.decided_level || levelText)}
      </div>`;
    }
    if (/告警冷却抑制/.test(msg)) {
      const typeText = detail.event_type_cn || this.agentEventLabel(detail.event_type) || msg.replace(/^告警冷却抑制:\s*/, '').trim();
      return `<div class="agent-log-line">
        <strong class="agent-log-action">冷却抑制</strong>
        ${this.agentLogChip('type', typeText)}
        <span class="agent-log-note">短时间内不重复推送</span>
      </div>`;
    }
    if (/智能体后台巡检/.test(msg)) {
      return `<div class="agent-log-line"><strong class="agent-log-action">后台巡检</strong><span class="agent-log-note">重检车牌 / 手势 / LLM / 数据库状态</span></div>`;
    }
    if (/可选配置未填写/.test(msg)) {
      return `<div class="agent-log-line"><strong class="agent-log-action">配置提示</strong><span class="agent-log-note">${this.escHtml(msg.replace(/^可选配置未填写:\s*/, ''))}</span></div>`;
    }
    return `<div class="agent-log-line"><span class="agent-log-note">${this.escHtml(this.humanizeAgentLogMessage(msg))}</span></div>`;
  },

  formatAlertLogDisplay(log) {
    const msg = log.message || '';
    const detail = log.detail_json || log.detail || {};
    const newFmt = msg.match(/^告警\s*#(\d+)\s*·\s*(.+?)\s*·\s*(.+?)\s*[—-]\s*(.+)$/s);
    const legacyFmt = msg.match(/^\[(.+?)\]\s*\[(.+?)\]\s*(.+?)\s*[—-]\s*(.+)$/s);
    if (detail.alert_id || newFmt || legacyFmt) {
      const alertId = detail.alert_id || newFmt?.[1];
      const typeText = detail.event_type_cn
        || (newFmt ? newFmt[2].trim() : '')
        || this.agentEventLabel(detail.event_type || legacyFmt?.[2]?.trim());
      const levelKey = detail.level || legacyFmt?.[1]?.trim();
      const levelText = detail.level_cn || this.agentLevelLabel(levelKey);
      const title = detail.title || (legacyFmt ? legacyFmt[3].trim() : (newFmt ? typeText : ''));
      const summary = detail.summary || newFmt?.[4]?.trim() || legacyFmt?.[4]?.trim() || '';
      return `<div class="alert-log-card">
        <div class="agent-log-line">
          ${alertId ? `<strong class="agent-log-action">告警 #${this.escHtml(String(alertId))}</strong>` : ''}
          ${this.agentLogChip('level', levelText, levelKey || levelText)}
          ${this.agentLogChip('type', typeText)}
        </div>
        ${title ? `<div class="alert-log-title">${this.escHtml(title)}</div>` : ''}
        ${summary ? `<div class="alert-log-summary">${this.escHtml(summary)}</div>` : ''}
      </div>`;
    }
    return `<div class="agent-log-line"><span class="agent-log-note">${this.escHtml(this.humanizeAgentLogMessage(msg))}</span></div>`;
  },

  humanizeAgentLogMessage(message) {
    let text = this.humanizeErrorText(message || '');
    Object.entries(this.AGENT_EVENT_LABELS).forEach(([key, label]) => {
      text = text.replace(new RegExp(key, 'g'), label);
    });
    text = text.replace(/\[(critical|warning|info)\]/gi, (_, lv) => `[${this.agentLevelLabel(lv)}]`);
    text = text.replace(/\bCRITICAL\b/g, '严重');
    text = text.replace(/\bWARNING\b/g, '警告');
    text = text.replace(/\bWARN\b/g, '警告');
    text = text.replace(/\bINFO\b/g, '信息');
    text = text.replace(/\bERROR\b/g, '错误');
    text = text.replace(/→\s*critical\b/gi, `→ ${this.agentLevelLabel('critical')}`);
    text = text.replace(/→\s*warning\b/gi, `→ ${this.agentLevelLabel('warning')}`);
    text = text.replace(/→\s*info\b/gi, `→ ${this.agentLevelLabel('info')}`);
    text = text.replace(/\['web',\s*'sse'\]/g, '网页 · SSE推送');
    text = text.replace(/\['web',\s*'webhook'\]/g, '网页 · Webhook');
    text = text.replace(/\['web',\s*'email'\]/g, '网页 · 邮件');
    text = text.replace(/→\s*\[([^\]]+)\]/g, (_, inner) => `→ ${this.agentChannelsText(`[${inner}]`)}`);
    return text;
  },

  severityKey(level) {
    const label = this.logLevelLabel(level);
    const byLabel = {
      '严重': 'critical', '错误': 'critical',
      '警告': 'warning',
      '信息': 'info', '提示': 'info',
      '调试': 'debug',
    };
    if (byLabel[label]) return byLabel[label];
    const raw = String(level || '').trim().toLowerCase();
    if (raw === 'critical' || raw === 'error' || raw === 'crit' || raw.includes('critical')) return 'critical';
    if (raw === 'warning' || raw === 'warn' || raw.includes('warning')) return 'warning';
    if (raw === 'info' || raw.includes('info')) return 'info';
    return 'info';
  },

  severityColor(level) {
    return this.SEVERITY_COLORS[this.severityKey(level)] || this._colors.muted;
  },

  renderLevelPill(level, label) {
    const key = this.severityKey(level);
    const text = label || this.logLevelLabel(level);
    return `<span class="level-pill severity-${key}">${this.escHtml(text)}</span>`;
  },

  logLevelLabel(level) {
    const map = {
      DEBUG: '调试', INFO: '信息', WARN: '警告', WARNING: '警告',
      ERROR: '错误', CRITICAL: '严重',
      debug: '调试', info: '信息', warning: '警告', error: '错误', critical: '严重',
    };
    return map[level] || level || '信息';
  },

  logLevelClass(level) {
    return this.severityKey(level);
  },

  logLevelMatchesFilter(logLevel, filterLevel) {
    if (!filterLevel) return true;
    return this.logLevelLabel(logLevel) === this.logLevelLabel(filterLevel);
  },

  getLogFilterParams() {
    return {
      cat: (document.getElementById('log-category') && document.getElementById('log-category').value) || '',
      level: (document.getElementById('log-level') && document.getElementById('log-level').value) || '',
      search: (document.getElementById('log-search') && document.getElementById('log-search').value) || '',
      start: (document.getElementById('log-start') && document.getElementById('log-start').value) || '',
      end: (document.getElementById('log-end') && document.getElementById('log-end').value) || '',
    };
  },

  logMatchesFilters(log, filters) {
    if (!log) return false;
    if (filters.cat && log.category !== filters.cat) return false;
    if (filters.level && !this.logLevelMatchesFilter(log.level, filters.level)) return false;
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
    const severity = this.severityKey(log.level);
    const levelLabel = this.logLevelLabel(log.level);
    const messageHtml = log.category === 'agent'
      ? this.formatAgentLogDisplay(log)
      : (log.category === 'alert'
        ? this.formatAlertLogDisplay(log)
        : this.escHtml(this.humanizeLogDisplay(log)));
    return `
      <div class="log-row severity-${severity}${liveClass}" onclick="App.showLogDetail('${this.escAttr(JSON.stringify(log))}')">
        <span>${new Date(log.created_at).toLocaleString()}</span>
        <span>${this.renderLevelPill(log.level, levelLabel)}</span>
        <span>${this.escHtml(this.logCategoryLabel(log.category))}</span>
        <span class="log-msg-cell">${messageHtml}</span>
        <span>${log.user_id || '-'}</span>
      </div>`;
  },

  renderLogTable(logs) {
    const table = document.getElementById('log-table');
    if (!table) return;
    table.innerHTML =
      '<div class="log-row header"><span>时间</span><span>级别</span><span>类别</span><span>消息</span><span>用户</span></div>' +
      (logs || []).map(l => this.renderLogRow(l, false)).join('') ||
      '<p style="padding:1rem;color:var(--text-muted);">暂无日志</p>';
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
      ${Object.entries(stats.by_level || {}).map(([k, v]) => {
        const key = this.severityKey(k);
        const label = this.logLevelLabel(k);
        return `<span class="level-pill severity-${key}">${this.escHtml(label)}: ${v}</span>`;
      }).join('')}
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
    this.logSseSource = new EventSource(this.monitorStreamUrl('/api/monitor/logs/stream'));
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
      const detail = log.detail_json;
      let detailText = '';
      if (detail && typeof detail === 'object') {
        if (detail.error_message) {
          detailText = `错误说明：${this.humanizeErrorText(detail.error_message)}`;
        } else if (detail.traceback) {
          detailText = '（技术堆栈已隐藏，仅保留摘要）';
        }
      }
      const lines = [
        `时间：${new Date(log.created_at).toLocaleString()}`,
        `级别：${this.logLevelLabel(log.level_cn || log.level)}`,
        `类别：${log.category_cn || this.logCategoryLabel(log.category)}`,
        `消息：${this.humanizeLogDisplay(log)}`,
      ];
      if (detailText) lines.push(detailText);
      if (log.user_id) lines.push(`用户ID：${log.user_id}`);
      alert(lines.join('\n'));
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
    if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.askAssistant(); });
    this.initAssistantVoice();
    this.renderAssistantHistory();
    this.setAssistantStatus('点击「立即巡检」查看系统状态');
    this.initAgentDrag();
    this.restoreAgentPosition();
    this.setAgentState('idle');
  },

  initAssistantVoice() {
    const saved = localStorage.getItem('assistantVoiceEnabled');
    this.assistantVoiceEnabled = saved === 'true';
    this.updateVoiceToggleUI();

    const loadVoices = () => {
      this._agentVoice = this.pickDoubaoStyleVoice();
    };
    if ('speechSynthesis' in window) {
      loadVoices();
      window.speechSynthesis.onvoiceschanged = loadVoices;
    }
  },

  /** 挑选接近豆包风格的自然中文语音（优先神经网络/在线音色） */
  pickDoubaoStyleVoice() {
    if (!('speechSynthesis' in window)) return null;
    const voices = window.speechSynthesis.getVoices();
    if (!voices.length) return null;

    const zhVoices = voices.filter(v => v.lang && (v.lang.startsWith('zh') || v.lang.includes('CN')));
    const pool = zhVoices.length ? zhVoices : voices;

    const avoid = /kangkang|yunjian|yunxi|yunze|male|guy|david|童|child|junior|yaoyao|huihui|kang/i;

    const priorityPatterns = [
      /xiaoxiao.*(neural|online|natural)/i,
      /晓晓.*(自然|在线|神经)/i,
      /xiaoyi.*(neural|online|natural)/i,
      /晓伊.*(自然|在线|神经)/i,
      /xiaoxuan.*neural/i,
      /xiaomo.*neural/i,
      /yunxia.*neural/i,
      /neural.*zh[- ]?cn/i,
      /online.*natural/i,
      /natural.*zh/i,
      /google.*普通话.*(中国|中国大陆)/i,
      /microsoft.*xiaoxiao/i,
      /microsoft.*xiaoyi/i,
    ];

    for (const pattern of priorityPatterns) {
      const hit = pool.find(v => pattern.test(v.name) && !avoid.test(v.name));
      if (hit) return hit;
    }

    const cloudNatural = pool.filter(v =>
      !v.localService && /xiaoxiao|xiaoyi|neural|natural|online|晓晓|晓伊/i.test(v.name) && !avoid.test(v.name)
    );
    if (cloudNatural.length) return cloudNatural[0];

    let best = null;
    let bestScore = -999;
    for (const v of pool) {
      if (avoid.test(v.name)) continue;
      const name = v.name;
      let score = 0;
      if (v.lang === 'zh-CN' || v.lang === 'cmn-CN') score += 10;
      if (/neural|natural|online/i.test(name)) score += 20;
      if (/xiaoxiao|xiaoyi|晓晓|晓伊/i.test(name)) score += 18;
      if (!v.localService) score += 8;
      if (/google|microsoft/i.test(name)) score += 4;
      if (score > bestScore) {
        bestScore = score;
        best = v;
      }
    }
    return best || pool.find(v => !avoid.test(v.name)) || pool[0];
  },

  onVoiceToggleChange(enabled) {
    this.assistantVoiceEnabled = !!enabled;
    localStorage.setItem('assistantVoiceEnabled', String(this.assistantVoiceEnabled));
    this.updateVoiceToggleUI();
    if (!this.assistantVoiceEnabled) {
      this.stopAssistantSpeech();
      this.setAssistantStatus('语音朗读已关闭');
    } else {
      this.setAssistantStatus('语音朗读已开启');
      this.speakAssistant('语音朗读已开启', { force: true });
    }
  },

  updateVoiceToggleUI() {
    const toggle = document.getElementById('assistant-voice-toggle');
    if (toggle) toggle.checked = this.assistantVoiceEnabled;
    const label = document.getElementById('voice-toggle-status');
    if (label) {
      label.textContent = this.assistantVoiceEnabled ? '朗读开' : '朗读关';
      label.classList.toggle('on', this.assistantVoiceEnabled);
    }
  },

  prepareSpeechText(text) {
    if (!text) return '';
    let t = this.stripMarkdown(text)
      .replace(/[🔔🔍⚠️💡✅🛠📋🧪•]/g, '')
      .replace(/\n+/g, '，')
      .replace(/\s+/g, ' ')
      .trim();
    if (t.length > 200) t = t.slice(0, 200) + '…';
    return t;
  },

  stopAssistantSpeech() {
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    this._agentSpeaking = false;
  },

  startAgentMonitorLoop() {
    this.stopAgentMonitorLoop();
    // 首次进入主动播报一次
    this.runAgentPatrol({ silent: true, speech: true, forceSpeech: true });
    this.agentMonitorTimer = setInterval(
      () => this.runAgentPatrol({ silent: true, speech: true }),
      30000
    );
  },

  stopAgentMonitorLoop() {
    if (this.agentMonitorTimer) {
      clearInterval(this.agentMonitorTimer);
      this.agentMonitorTimer = null;
    }
  },

  updateAgentLiveStatus(briefing) {
    const dot = document.getElementById('agent-live-dot');
    const text = document.getElementById('agent-live-text');
    if (!dot || !text || !briefing) return;

    const open = briefing.open_alerts || 0;
    const warnLogs = (briefing.logs_24h && briefing.logs_24h.warn_or_above) || 0;
    let state = 'ok';
    if (open > 0 || warnLogs > 0) state = 'warn';
    if (open >= 3) state = 'critical';

    dot.className = 'agent-live-dot ' + state;
    text.textContent = open > 0
      ? `监控中 · 有 ${open} 个问题待处理`
      : `一切正常 · 近24小时 ${(briefing.logs_24h && briefing.logs_24h.total) || 0} 次记录`;
  },

  async runAgentPatrol(opts = {}) {
    const { silent = false, speech = false, forceSpeech = false } = opts;
    if (!silent) {
      this.setAgentState('thinking');
      this.setAssistantStatus('正在巡检系统…', true);
    }
    try {
      const data = await this.api('/api/monitor/agent/briefing');
      this.agentBriefing = data;
      this.agentOpenCount = data.open_alerts || 0;
      this.updateAgentBadge();
      this.updateAgentLiveStatus(data);

      const briefKey = `${data.open_alerts}|${(data.logs_24h && data.logs_24h.warn_or_above) || 0}`;
      const statusChanged = briefKey !== this.agentLastBriefKey;
      this.agentLastBriefKey = briefKey;

      const subtitle = document.getElementById('agent-subtitle');
      if (subtitle) {
        subtitle.textContent = data.open_alerts > 0
          ? `发现 ${data.open_alerts} 条未处理告警`
          : '持续监听三路识别与用户操作';
      }

      if (data.open_alerts > 0) {
        this.setAgentState('warning');
      } else if (!this.assistantThinking) {
        this.setAgentState('idle');
      }

      // 仅在状态变化或首次/手动巡检时说话，避免每 30 秒重复播报
      if (speech && (forceSpeech || statusChanged)) {
        const short = data.open_alerts > 0
          ? `提醒您，还有 ${data.open_alerts} 个问题待处理哦`
          : (statusChanged ? '系统运行正常，我会继续帮您看着' : '');
        if (short) this.showAgentSpeech(short, 5000);
      }

      if (!silent) {
        const summary = data.summary_user || data.summary;
        this.addAssistantMessage('assistant', summary);
        if (this.assistantVoiceEnabled) this.speakAssistant(summary);
        if (data.recent_alerts && data.recent_alerts.length) {
          const list = data.recent_alerts.slice(0, 3).map(a =>
            `• ${a.summary_user || a.title || a.event_type_user}`
          ).join('\n');
          this.addAssistantMessage('assistant', `最近的情况：\n${list}\n\n若要问某一条的根因或处理方式，请先在告警中心点「回放」选定，或点击上方最新告警卡片。`);
        }
        this.updateAssistantContextUI();
        this.setAssistantStatus('巡检完成，有问题可以继续问我');
      }

      return data;
    } catch (e) {
      if (!silent) {
        this.addAssistantMessage('assistant', `巡检失败：${e.message}`);
        this.setAssistantStatus('巡检失败，请确认后端服务已启动');
      }
      const text = document.getElementById('agent-live-text');
      if (text) text.textContent = '监控连接失败，请检查服务';
      const dot = document.getElementById('agent-live-dot');
      if (dot) dot.className = 'agent-live-dot critical';
      return null;
    } finally {
      if (!silent && !this.assistantThinking) this.setAgentState(this.agentOpenCount > 0 ? 'warning' : 'idle');
    }
  },

  async agentTriggerTestAlert() {
    this.setAgentState('thinking');
    this.setAssistantStatus('正在触发测试告警…', true);
    try {
      const data = await this.api('/api/monitor/alerts/test', { method: 'POST' });
      const userMsg = `我帮您发了一条测试提醒：「${data.title}」。${data.summary || ''}`;
      this.addAssistantMessage('assistant', userMsg);
      this.showAgentSpeech('已发送一条测试提醒，您可以体验完整流程', 4000);
      this.onAgentAlert({ id: data.id, level: data.level || 'info', title: data.title, summary: data.summary, suggestion: data.suggestion, event_type: data.event_type });
      this.setAssistantStatus('测试提醒已发出，请到告警中心查看');
    } catch (e) {
      this.addAssistantMessage('assistant', `触发失败：${e.message}`);
      this.setAssistantStatus('触发失败');
    } finally {
      if (!this.assistantThinking) this.setAgentState(this.agentOpenCount > 0 ? 'warning' : 'idle');
    }
  },

  initAgentDrag() {
    const bot = document.getElementById('assistant-bot');
    const avatar = document.getElementById('agent-avatar-wrap');
    if (!bot || !avatar) {
      console.warn('Alert Agent: 未找到智能体 DOM 元素');
      return;
    }

    if (avatar.dataset.bound === '1') return;
    avatar.dataset.bound = '1';

    const clampPosition = (left, top) => {
      const w = Math.max(bot.offsetWidth, 110);
      const h = Math.max(bot.offsetHeight, 110);
      return {
        left: Math.max(8, Math.min(window.innerWidth - w - 8, left)),
        top: Math.max(8, Math.min(window.innerHeight - h - 8, top)),
      };
    };

    const applyPosition = (left, top) => {
      const pos = clampPosition(left, top);
      bot.style.left = `${pos.left}px`;
      bot.style.top = `${pos.top}px`;
      bot.style.right = 'auto';
      bot.style.bottom = 'auto';
    };

    const onPointerDown = (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();

      const rect = bot.getBoundingClientRect();
      this._agentDrag = {
        pointerId: e.pointerId,
        offsetX: e.clientX - rect.left,
        offsetY: e.clientY - rect.top,
        startX: e.clientX,
        startY: e.clientY,
        moved: false,
      };
      this._agentPointerId = e.pointerId;
      bot.classList.add('dragging');
      avatar.classList.add('dragging');
      avatar.setPointerCapture(e.pointerId);
    };

    const onPointerMove = (e) => {
      if (!this._agentDrag || e.pointerId !== this._agentDrag.pointerId) return;
      const dx = Math.abs(e.clientX - this._agentDrag.startX);
      const dy = Math.abs(e.clientY - this._agentDrag.startY);
      if (dx > 5 || dy > 5) {
        this._agentDrag.moved = true;
        this.agentDragMoved = true;
      }
      if (!this._agentDrag.moved) return;
      e.preventDefault();
      applyPosition(e.clientX - this._agentDrag.offsetX, e.clientY - this._agentDrag.offsetY);
    };

    const onPointerUp = (e) => {
      if (!this._agentDrag || e.pointerId !== this._agentDrag.pointerId) return;
      const wasMoved = this._agentDrag.moved;
      bot.classList.remove('dragging');
      avatar.classList.remove('dragging');
      try { avatar.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }

      if (wasMoved) {
        this.saveAgentPosition();
      } else {
        this.toggleAssistant();
      }

      this._agentDrag = null;
      this._agentPointerId = null;
      setTimeout(() => { this.agentDragMoved = false; }, 0);
    };

    avatar.addEventListener('pointerdown', onPointerDown);
    avatar.addEventListener('pointermove', onPointerMove);
    avatar.addEventListener('pointerup', onPointerUp);
    avatar.addEventListener('pointercancel', onPointerUp);

    // 兼容旧浏览器：无 Pointer Events 时回退到 mouse
    if (!window.PointerEvent) {
      avatar.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        const rect = bot.getBoundingClientRect();
        this._agentDrag = {
          offsetX: e.clientX - rect.left,
          offsetY: e.clientY - rect.top,
          startX: e.clientX,
          startY: e.clientY,
          moved: false,
        };
        bot.classList.add('dragging');
        const onMouseMove = (ev) => {
          if (!this._agentDrag) return;
          if (Math.abs(ev.clientX - this._agentDrag.startX) > 5 || Math.abs(ev.clientY - this._agentDrag.startY) > 5) {
            this._agentDrag.moved = true;
          }
          if (this._agentDrag.moved) {
            applyPosition(ev.clientX - this._agentDrag.offsetX, ev.clientY - this._agentDrag.offsetY);
          }
        };
        const onMouseUp = () => {
          if (!this._agentDrag) return;
          const moved = this._agentDrag.moved;
          bot.classList.remove('dragging');
          if (moved) this.saveAgentPosition();
          else this.toggleAssistant();
          this._agentDrag = null;
          document.removeEventListener('mousemove', onMouseMove);
          document.removeEventListener('mouseup', onMouseUp);
        };
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
      });
    }
  },

  saveAgentPosition() {
    const bot = document.getElementById('assistant-bot');
    if (!bot) return;
    const rect = bot.getBoundingClientRect();
    if (rect.left >= 0 && rect.top >= 0 && rect.left < window.innerWidth - 40 && rect.top < window.innerHeight - 40) {
      localStorage.setItem('agentPosition', JSON.stringify({ left: rect.left, top: rect.top }));
    }
  },

  restoreAgentPosition() {
    const bot = document.getElementById('assistant-bot');
    const raw = localStorage.getItem('agentPosition');
    if (!bot || !raw) return;
    try {
      const pos = JSON.parse(raw);
      if (typeof pos.left === 'number' && typeof pos.top === 'number') {
        const w = Math.max(bot.offsetWidth, 110);
        const h = Math.max(bot.offsetHeight, 110);
        const inView = pos.left >= -20 && pos.top >= -20
          && pos.left < window.innerWidth - 40
          && pos.top < window.innerHeight - 40;
        if (inView) {
          bot.style.left = `${pos.left}px`;
          bot.style.top = `${pos.top}px`;
          bot.style.right = 'auto';
          bot.style.bottom = 'auto';
        } else {
          localStorage.removeItem('agentPosition');
        }
      }
    } catch (e) {
      localStorage.removeItem('agentPosition');
    }
  },

  setAgentState(state) {
    const ring = document.getElementById('agent-status-ring');
    const wrap = document.getElementById('agent-avatar-wrap');
    const s = state || 'idle';
    if (ring) ring.className = 'agent-status-ring agent-state-' + s;
    if (wrap) {
      const wasActive = wrap.classList.contains('active');
      const moodMap = {
        idle: 'idle',
        info: 'idle',
        thinking: 'thinking',
        listening: 'listening',
        speaking: 'speaking',
        warning: 'error',
        critical: 'error',
        error: 'error',
      };
      wrap.className = 'agent-avatar-wrap agent-mood-' + (moodMap[s] || 'idle');
      if (wasActive) wrap.classList.add('active');
    }
  },

  showAgentSpeech(text, duration = 6000) {
    const el = document.getElementById('agent-speech');
    if (!el) return;
    el.textContent = text;
    el.classList.add('visible');
    if (this.agentSpeechTimer) clearTimeout(this.agentSpeechTimer);
    this.agentSpeechTimer = setTimeout(() => el.classList.remove('visible'), duration);
    if (this.assistantVoiceEnabled) this.speakAssistant(text);
  },

  onAgentAlert(alert) {
    const level = alert.level || 'info';
    this.setAgentState(level);
    if (alert.id) this.setFocusedAlert(alert);
    this.refreshAgentStats();

    const levelLabel = { info: '提示', warning: '需要注意', critical: '比较紧急' }[level] || '提醒';
    const deferSpeechForDrivingAdvice = alert.event_type && (
      alert.event_type.startsWith('scenario_')
      || alert.event_type.startsWith('fusion_')
      || alert.event_type === 'owner_action_suppressed'
    );
    if (!deferSpeechForDrivingAdvice) {
      this.showAgentSpeech(this.alertToUserSpeech(alert), 8000);
    }

    const latest = document.getElementById('agent-latest-alert');
    if (latest) {
      latest.className = 'agent-latest-alert ' + level;
      latest.innerHTML = `<strong>${this.escHtml(alert.title || '系统提醒')}</strong>${this.escHtml(alert.summary || '')}${alert.suggestion ? '<br><em>建议：' + this.escHtml(alert.suggestion) + '</em>' : ''}<br><span class="agent-latest-hint">点击此卡片 · 设为当前讨论告警</span>`;
      latest.classList.remove('hidden');
      latest.onclick = () => this.setFocusedAlert(alert);
      latest.title = '点击将此告警设为当前讨论对象';
    }

    const subtitle = document.getElementById('agent-subtitle');
    if (subtitle) subtitle.textContent = `刚发现：${alert.title || '系统异常'}`;

    const userLines = [alert.summary || alert.title || '系统检测到一项异常'];
    if (alert.suggestion) userLines.push('建议：' + alert.suggestion);
    const alertMsg = `🔔 ${levelLabel}提醒\n${userLines.join('\n')}`;
    this.addAssistantMessage('assistant', alertMsg);
    this.loadAgentActivity();
    if (
      alert.event_type && (
        alert.event_type.startsWith('scenario_')
        || alert.event_type.startsWith('fusion_')
        || alert.event_type === 'owner_action_suppressed'
      )
    ) {
      this.loadScenarioFusion({ fromAlert: true });
    }

    if (level === 'critical') {
      this.toggleAssistant(true);
    }

    setTimeout(() => {
      if (!this.assistantThinking) this.setAgentState(this.agentOpenCount > 0 ? 'warning' : 'idle');
    }, 12000);
  },

  updateAgentBadge() {
    const badge = document.getElementById('agent-badge');
    if (!badge) return;
    if (this.agentOpenCount > 0) {
      badge.textContent = this.agentOpenCount > 99 ? '99+' : String(this.agentOpenCount);
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  },

  async refreshAgentStats() {
    try {
      const stats = await this.api('/api/monitor/alerts/stats');
      this.agentOpenCount = stats.open || 0;
      this.updateAgentBadge();
      if (stats.open > 0) {
        this.setAgentState('warning');
        this.showAgentSpeech(`当前有 ${stats.open} 条未处理告警`, 5000);
      }
    } catch (e) { /* ignore */ }
  },

  goToAlerts() {
    const nav = document.querySelector('.nav-item[data-view="alerts"]');
    if (nav) nav.click();
    const panel = document.getElementById('assistant-panel');
    if (panel) panel.classList.add('hidden');
  },

  toggleAssistant(forceOpen) {
    const panel = document.getElementById('assistant-panel');
    const avatar = document.getElementById('agent-avatar-wrap');
    if (!panel || !avatar) return;

    let isHidden;
    if (forceOpen === true) {
      isHidden = false;
      panel.classList.remove('hidden');
    } else if (forceOpen === false) {
      isHidden = true;
      panel.classList.add('hidden');
    } else {
      isHidden = panel.classList.toggle('hidden');
    }

    avatar.classList.toggle('active', !isHidden);
    if (!isHidden) {
      this.renderAssistantHistory();
      this.refreshAgentStats();
      this.updateAssistantContextUI();
      if (this.assistantHistory.length === 0) {
        this.addAssistantMessage('assistant',
          '你好，我是小智，您的系统助手。\n\n' +
          '我会帮您盯着车牌识别、手势识别和账号安全。有问题我会用大白话告诉您，不用懂技术也能明白。\n\n' +
          '您可以：\n' +
          '• 点「立即巡检」—— 我帮您看看系统是否正常\n' +
          '• 点「模拟告警」—— 体验我会怎么提醒您\n' +
          '• 在告警中心「回放」选定一条后，再点根因/建议/影响\n' +
          '• 直接问我「系统正常吗」这类整体问题'
        );
      }
      this.runAgentPatrol({ silent: true });
      setTimeout(() => {
        document.addEventListener('click', this.closeAssistantOnOutsideClick);
      }, 0);
    } else {
      document.removeEventListener('click', this.closeAssistantOnOutsideClick);
    }
  },

  closeAssistantOnOutsideClick(evt) {
    const panel = document.getElementById('assistant-panel');
    const avatar = document.getElementById('agent-avatar-wrap');
    if (!panel || !avatar) return;
    if (evt.target instanceof Element && !panel.contains(evt.target) && !avatar.contains(evt.target)) {
      panel.classList.add('hidden');
      avatar.classList.remove('active');
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
    if (this.assistantHistory.length > 50) this.assistantHistory.shift();
    this.renderAssistantHistory();
  },

  renderAssistantHistory() {
    const box = document.getElementById('assistant-history');
    if (!box) return;
    box.innerHTML = this.assistantHistory.map(msg => `
      <div class="assistant-msg ${msg.role}">
        <div class="assistant-msg-bubble">${msg.role === 'assistant' ? this.formatAssistantText(msg.content) : this.escHtml(msg.content)}</div>
      </div>
    `).join('');
    box.scrollTop = box.scrollHeight;
  },

  async askAssistant(question, intent) {
    const input = document.getElementById('assistant-input');
    const q = typeof question === 'string' ? question : (input && input.value && input.value.trim());
    if (!q || this.assistantThinking) return;

    const body = this.buildAssistantPayload(q, intent);
    this.addAssistantMessage('user', q);
    if (input) input.value = '';
    this.assistantThinking = true;
    this.setAssistantStatus('Alert Agent 正在分析...', true);
    this.setAgentState('thinking');

    const panel = document.getElementById('assistant-panel');
    if (panel) panel.classList.add('assistant-processing');

    try {
      const data = await this.api('/api/monitor/assistant', { method: 'POST', body: JSON.stringify(body) });
      const answer = data.answer || '我暂时没想好怎么说，您可以换个方式问问，或者先点「立即巡检」。';
      this.addAssistantMessage('assistant', answer);
      const aiMode = data.ai?.mode === 'llm' ? `${data.ai.provider || 'AI'} · ${data.ai.model || '大模型'}` : '本地模板降级';
      const aiHint = data.ai?.hint ? ` · ${data.ai.hint}` : '';
      if (data.needs_clarification) {
        this.setAssistantStatus('请先选定一条告警');
      } else if (this.focusedAlert) {
        this.setAssistantStatus(`正在讨论：${this.focusedAlert.title} · ${aiMode}${aiHint}`);
      } else {
        this.setAssistantStatus(`回答完成 · ${aiMode}${aiHint}`);
      }
      this.setAgentState('idle');
      if (this.assistantVoiceEnabled) this.speakAssistant(answer);
    } catch (e) {
      this.addAssistantMessage('assistant', `抱歉，我没能连上后台：${e.message}。请确认系统已启动后再试。`);
      this.setAssistantStatus('请求失败，请稍后重试');
      this.setAgentState('warning');
    } finally {
      this.assistantThinking = false;
      if (panel) panel.classList.remove('assistant-processing');
    }
  },

  startPanelDrag(evt) {
    const panel = document.getElementById('assistant-panel');
    if (!panel || evt.target.closest('.assistant-close') || evt.target.closest('.assistant-icon-btn') || evt.target.closest('button') || evt.target.closest('input') || evt.target.closest('.assistant-history')) return;
    evt.preventDefault();
    this.dragOffsetX = evt.clientX - panel.getBoundingClientRect().left;
    this.dragOffsetY = evt.clientY - panel.getBoundingClientRect().top;
    panel.classList.add('dragging');
    const onMouseMove = (moveEvt) => {
      panel.style.left = `${moveEvt.clientX - this.dragOffsetX}px`;
      panel.style.top = `${moveEvt.clientY - this.dragOffsetY}px`;
      panel.style.right = 'auto'; panel.style.bottom = 'auto';
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
    if (!SpeechRecognition) { this.setAssistantStatus('当前浏览器不支持语音输入'); return; }
    if (this.assistantRecognition) { this.assistantRecognition.stop(); return; }
    const recognition = new SpeechRecognition();
    recognition.lang = 'zh-CN'; recognition.continuous = false; recognition.interimResults = false;
    this.assistantRecognition = recognition;
    this.setAgentState('listening');
    recognition.onstart = () => this.setAssistantStatus('正在聆听...', false);
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results).map(r => r[0].transcript).join('');
      const inp = document.getElementById('assistant-input');
      if (inp) inp.value = transcript;
      this.askAssistant(transcript);
    };
    recognition.onerror = (e) => { this.setAssistantStatus(`语音输入失败: ${e.error}`); };
    recognition.onend = () => {
      this.assistantRecognition = null;
      if (!this.assistantThinking) {
        this.setAgentState('idle');
        this.setAssistantStatus('准备就绪');
      }
    };
    recognition.start();
  },

  speakAssistant(text, opts = {}) {
    const force = opts.force === true;
    if (!force && !this.assistantVoiceEnabled) return;
    if (!('speechSynthesis' in window) || !text) return;

    const prepared = this.prepareSpeechText(text);
    if (!prepared) return;

    this.stopAssistantSpeech();
    if (!this._agentVoice) this._agentVoice = this.pickDoubaoStyleVoice();

    const utterance = new SpeechSynthesisUtterance(prepared);
    utterance.lang = 'zh-CN';
    utterance.pitch = 1.0;
    utterance.rate = 0.92;
    utterance.volume = 1.0;
    if (this._agentVoice) utterance.voice = this._agentVoice;

    const prevState = this.agentOpenCount > 0 ? 'warning' : 'idle';
    this._agentSpeaking = true;
    if (!this.assistantThinking) this.setAgentState('speaking');

    utterance.onend = () => {
      this._agentSpeaking = false;
      if (!this.assistantThinking) {
        this.setAgentState(this.agentOpenCount > 0 ? 'warning' : prevState);
      }
    };
    utterance.onerror = () => {
      this._agentSpeaking = false;
      if (!this.assistantThinking) this.setAgentState(prevState);
    };

    window.speechSynthesis.speak(utterance);
  },

  speakLastAnswer() {
    const last = [...this.assistantHistory].reverse().find(msg => msg.role === 'assistant');
    if (last) this.speakAssistant(last.content, { force: true });
  },

  initRecognitionMirrors() {
    if (!this.recognitionMirrors) this.recognitionMirrors = {};
    const defaults = {
      lpr: { status: 'idle', statusText: '未运行', source: '车牌识别模块', previewSrc: '', result: null, updatedAt: null },
      police: { status: 'idle', statusText: '未运行', source: '交警手势模块', previewSrc: '', result: null, updatedAt: null },
      owner: { status: 'idle', statusText: '未运行', source: '车主控车模块', previewSrc: '', result: null, vehicleState: null, updatedAt: null },
    };
    Object.entries(defaults).forEach(([module, fallback]) => {
      this.recognitionMirrors[module] = { ...fallback, ...(this.recognitionMirrors[module] || {}) };
      this.renderRecognitionMirror(module);
    });
    const ownerState = this.recognitionMirrors.owner?.vehicleState || this.ownerVehicleState;
    if (ownerState) this.publishOwnerVehicleState(ownerState);
    if (this.loadVehicleState) {
      Promise.resolve(this.loadVehicleState()).catch(() => {});
    }
  },

  setRecognitionMirrorStatus(module, status, statusText, source, options = {}) {
    if (!['lpr', 'police', 'owner'].includes(module)) return;
    if (!this.recognitionMirrors) this.initRecognitionMirrors();
    const defaults = { lpr: '车牌识别模块', police: '交警手势模块', owner: '车主控车模块' };
    const state = this.recognitionMirrors[module] || {
      status: 'idle', statusText: '未运行', source: defaults[module], previewSrc: '', result: null, updatedAt: null,
    };
    state.status = status || state.status || 'idle';
    state.statusText = statusText || ({
      idle: '未运行', connecting: '正在连接', running: '实时识别中', complete: '识别已完成', error: '识别异常',
    }[state.status] || state.statusText || '未运行');
    if (source !== undefined && source !== null) state.source = String(source).trim() || defaults[module];
    if (options.clearPreview) {
      state.previewSrc = '';
      state.previewFailed = false;
      state.result = null;
    } else if (Object.prototype.hasOwnProperty.call(options, 'previewUrl')) {
      const rawPreview = String(options.previewUrl || '').trim();
      const nextPreview = rawPreview && !/^(?:data:|blob:|https?:\/\/)/i.test(rawPreview) && this.apiUrl
        ? this.apiUrl(rawPreview)
        : rawPreview;
      if (nextPreview !== state.previewSrc) state.previewFailed = false;
      state.previewSrc = nextPreview;
    }
    state.updatedAt = new Date().toISOString();
    this.recognitionMirrors[module] = state;
    this.renderRecognitionMirror(module);
  },

  publishRecognitionResult(module, data = {}, options = {}) {
    if (!['lpr', 'police', 'owner'].includes(module)) return;
    if (!this.recognitionMirrors) this.initRecognitionMirrors();
    const state = this.recognitionMirrors[module] || {};
    this.setRecognitionMirrorStatus(
      module,
      options.status || state.status || 'running',
      options.statusText,
      options.source,
      Object.prototype.hasOwnProperty.call(options, 'previewUrl') ? { previewUrl: options.previewUrl } : {},
    );
    const current = this.recognitionMirrors[module];
    current.result = data || {};
    const annotatedImage = data?.annotated_image;
    if (annotatedImage) {
      const rawImage = String(annotatedImage);
      current.previewSrc = /^data:image\//i.test(rawImage) ? rawImage : 'data:image/jpeg;base64,' + rawImage;
    }
    current.updatedAt = new Date().toISOString();
    const acceptVehicleState = options.acceptVehicleState !== false;
    if (module === 'owner' && data?.vehicle_state && acceptVehicleState) current.vehicleState = { ...data.vehicle_state };
    this.renderRecognitionMirror(module);
    if (module === 'owner' && data?.vehicle_state && acceptVehicleState) this.publishOwnerVehicleState(data.vehicle_state);
    this.scheduleScenarioFusionRefresh?.(550);
  },

  renderRecognitionMirror(module) {
    const state = this.recognitionMirrors?.[module];
    if (!state) return;
    const statusEl = document.getElementById('mirror-' + module + '-status');
    const sourceEl = document.getElementById('mirror-' + module + '-source');
    const imageEl = document.getElementById('mirror-' + module + '-image');
    const placeholderEl = document.getElementById('mirror-' + module + '-placeholder');
    const resultEl = document.getElementById('mirror-' + module + '-result');
    if (statusEl) {
      statusEl.dataset.state = state.status || 'idle';
      statusEl.textContent = state.statusText || '未运行';
    }
    if (sourceEl) sourceEl.textContent = '来源：' + (state.source || this.recognitionMirrorSource(module));
    if (imageEl && state.previewSrc) {
      let renderedSrc = state.previewSrc;
      if (state.previewFailed && /^https?:\/\//i.test(renderedSrc)) {
        const separator = renderedSrc.includes('?') ? '&' : '?';
        renderedSrc += separator + '_mirror_retry=' + Date.now();
        state.previewFailed = false;
      }
      if (imageEl.getAttribute('src') !== renderedSrc) imageEl.src = renderedSrc;
      imageEl.hidden = false;
      imageEl.onerror = () => {
        state.previewFailed = true;
        imageEl.hidden = true;
        if (placeholderEl) {
          placeholderEl.hidden = false;
          placeholderEl.textContent = '识别画面暂不可用';
        }
      };
      imageEl.onload = () => { state.previewFailed = false; };
      if (placeholderEl) placeholderEl.hidden = true;
    } else {
      if (imageEl) {
        imageEl.hidden = true;
        imageEl.removeAttribute('src');
      }
      if (placeholderEl) {
        const label = ({ lpr: '车牌识别', police: '交警手势', owner: '车主控车' })[module] || module;
        placeholderEl.hidden = false;
        placeholderEl.textContent = state.status === 'connecting'
          ? '正在等待识别画面'
          : (state.status === 'error' ? '识别画面不可用' : '请在' + label + '模块启动识别');
      }
    }
    if (!resultEl) return;
    const data = state.result;
    if (!data) {
      resultEl.textContent = state.status === 'error' ? (state.statusText || '识别异常') : '尚无识别结果';
      return;
    }
    let title = '';
    let detail = '';
    if (module === 'lpr') {
      const plates = Array.isArray(data.plates) ? data.plates : [];
      const labels = plates.map(item => item?.plate_number || item?.plate || '').filter(Boolean);
      title = labels.length ? labels.join('、') : '当前画面未识别到车牌';
      detail = labels.length ? '检测到 ' + (data.plate_count ?? labels.length) + ' 个车牌' : '持续识别中';
    } else if (module === 'police') {
      title = data.gesture_cn || data.gesture || '当前无明确交警手势';
      const confidence = Number(data.confidence);
      detail = Number.isFinite(confidence) ? '置信度 ' + (confidence * 100).toFixed(0) + '%' : '持续识别中';
    } else {
      title = data.gesture_cn || data.gesture || '当前无明确车主手势';
      const action = data.action ? (this.ownerActionLabel ? this.ownerActionLabel(data.action) : data.action) : '';
      const confidence = Number(data.confidence);
      detail = [
        action ? '动作：' + action : '',
        Number.isFinite(confidence) ? '置信度 ' + (confidence * 100).toFixed(0) + '%' : '',
      ].filter(Boolean).join(' · ') || '持续识别中';
    }
    const updatedAt = state.updatedAt ? new Date(state.updatedAt).toLocaleTimeString() : '';
    resultEl.innerHTML = '<strong>' + this.escHtml(title) + '</strong><br>'
      + '<span>' + this.escHtml(detail) + '</span>'
      + (updatedAt ? '<br><small>更新于 ' + this.escHtml(updatedAt) + '</small>' : '');
  },

  publishOwnerVehicleState(vehicleState) {
    if (!vehicleState) return;
    if (!this.recognitionMirrors) this.initRecognitionMirrors();
    const owner = this.recognitionMirrors.owner || {};
    owner.vehicleState = { ...vehicleState };
    owner.updatedAt = new Date().toISOString();
    this.recognitionMirrors.owner = owner;
    this.renderOwnerVehicleMirror(owner.vehicleState);
  },

  renderOwnerVehicleMirror(vehicleState = null) {
    const state = vehicleState || this.recognitionMirrors?.owner?.vehicleState || this.ownerVehicleState;
    if (!state) return;
    const awake = Boolean(state.is_awake);
    const volumeValue = Number(state.volume);
    const temperatureValue = Number(state.temperature);
    const volume = Number.isFinite(volumeValue) ? Math.max(0, Math.min(100, volumeValue)) : 50;
    const temperature = Number.isFinite(temperatureValue) ? Math.max(16, Math.min(32, temperatureValue)) : 24;
    const phoneInCall = state.phone_status === 'in_call';
    const names = {
      volume_up: '音量 +', volume_down: '音量 -', temp_up: '温度 +', temp_down: '温度 -', standby: '待机主页',
    };
    const current = state.current_page || 'volume_up';
    const controlItems = ['volume_up', 'volume_down', 'temp_up', 'temp_down'];
    const selectedControl = controlItems.includes(current) ? current : 'volume_up';
    const awakeEl = document.getElementById('mirror-owner-awake');
    const volumeEl = document.getElementById('mirror-owner-volume');
    const volumeFill = document.getElementById('mirror-owner-volume-fill');
    const temperatureEl = document.getElementById('mirror-owner-temperature');
    const temperatureFill = document.getElementById('mirror-owner-temperature-fill');
    const phoneEl = document.getElementById('mirror-owner-phone');
    const selectionEl = document.getElementById('mirror-owner-selection');
    if (awakeEl) {
      awakeEl.textContent = awake ? '已唤醒' : '休眠';
      awakeEl.classList.toggle('awake', awake);
    }
    if (volumeEl) volumeEl.textContent = String(volume);
    if (volumeFill) volumeFill.style.width = volume + '%';
    if (temperatureEl) temperatureEl.textContent = temperature + '°C';
    if (temperatureFill) temperatureFill.style.width = (((temperature - 16) / 16) * 100).toFixed(1) + '%';
    if (phoneEl) {
      phoneEl.textContent = phoneInCall ? '通话中' : '空闲';
      phoneEl.classList.toggle('in-call', phoneInCall);
      phoneEl.classList.toggle('idle', !phoneInCall);
    }
    if (selectionEl) selectionEl.textContent = names[selectedControl] || selectedControl;
    document.querySelectorAll('.mirror-control-chip[data-control]').forEach(chip => {
      const selected = chip.dataset.control === selectedControl;
      chip.classList.toggle('active', selected);
      chip.setAttribute('aria-current', selected ? 'true' : 'false');
    });
  },

  recognitionMirrorSource(module, fallback = '') {
    const defaults = { lpr: '车牌识别模块', police: '交警手势模块', owner: '车主控车模块' };
    const source = String(this.recognitionMirrors?.[module]?.source || '').trim();
    if (source && source !== defaults[module]) return source;
    return fallback || source || defaults[module] || '';
  },

  scheduleScenarioFusionRefresh(delay = 550) {
    const state = this.scenarioFusionRefresh || (this.scenarioFusionRefresh = {
      timer: null,
      queuedAt: 0,
      inFlight: false,
      dirty: false,
    });
    const now = Date.now();
    if (!state.queuedAt) state.queuedAt = now;
    if (state.timer) clearTimeout(state.timer);
    const elapsed = now - state.queuedAt;
    const wait = elapsed >= 800 ? 0 : Math.min(Math.max(300, delay), 800 - elapsed);
    state.timer = setTimeout(() => {
      state.timer = null;
      state.queuedAt = 0;
      this.runScenarioFusionRefresh();
    }, wait);
  },

  async runScenarioFusionRefresh() {
    const state = this.scenarioFusionRefresh || (this.scenarioFusionRefresh = {
      timer: null,
      queuedAt: 0,
      inFlight: false,
      dirty: false,
    });
    if (state.inFlight) {
      state.dirty = true;
      return;
    }
    if (this.currentView !== 'alerts' || !this.loadScenarioFusion) return;
    state.inFlight = true;
    state.dirty = false;
    try {
      await this.loadScenarioFusion();
    } finally {
      state.inFlight = false;
      if (state.dirty) {
        state.dirty = false;
        this.scheduleScenarioFusionRefresh(300);
      }
    }
  },

  connectAlertScenarioLogStream() {
    if (this.alertScenarioLogSse || !document.getElementById('view-alerts')) return;
    const source = new EventSource(this.monitorStreamUrl('/api/monitor/logs/stream'));
    this.alertScenarioLogSse = source;
    source.onmessage = event => {
      let data;
      try { data = JSON.parse(event.data || '{}'); } catch (e) { return; }
      const category = data.category || data.data?.category;
      if (['lpr', 'police_gesture', 'owner_gesture'].includes(category)) {
        this.scheduleScenarioFusionRefresh(550);
      }
      if (category === 'owner_gesture' && this.loadVehicleState) {
        if (this.ownerVehicleRefreshTimer) clearTimeout(this.ownerVehicleRefreshTimer);
        this.ownerVehicleRefreshTimer = setTimeout(() => {
          this.ownerVehicleRefreshTimer = null;
          if (this.currentView === 'alerts') this.loadVehicleState();
        }, 350);
      }
    };
    // EventSource 会自动重连；仅在离开告警页时主动关闭。
    source.onerror = () => {};
  },

  disconnectAlertScenarioLogStream() {
    if (this.alertScenarioLogSse) {
      this.alertScenarioLogSse.close();
      this.alertScenarioLogSse = null;
    }
    if (this.ownerVehicleRefreshTimer) {
      clearTimeout(this.ownerVehicleRefreshTimer);
      this.ownerVehicleRefreshTimer = null;
    }
  },

  isIdleDrivingAdvice(advice) {
    if (!advice || !advice.advice) return true;
    const text = String(advice.advice).trim();
    if (!text || text === '暂无建议') return true;
    return text.includes('暂无明显驾驶相关信号');
  },

  notifyAgentDrivingAdvice(advice, opts = {}) {
    if (this.isIdleDrivingAdvice(advice)) return;
    const text = String(advice.advice).trim();
    const key = `${text}|${advice.signals_summary || ''}`;
    const force = opts.force === true;
    if (!force && key === this._lastAgentDrivingAdviceKey) return;
    this._lastAgentDrivingAdviceKey = key;

    const priority = advice.priority || 'normal';
    const duration = priority === 'high' ? 10000 : (priority === 'medium' ? 8000 : 6000);
    const speech = advice.signals_summary
      ? `🧭 ${text}\n信号：${advice.signals_summary}`
      : `🧭 ${text}`;

    if (priority === 'high') {
      this.setAgentState('warning');
      this.toggleAssistant(true);
    } else if (priority === 'medium') {
      this.setAgentState('info');
    }
    this.showAgentSpeech(speech, duration);

    const latest = document.getElementById('agent-latest-alert');
    if (latest) {
      const levelClass = priority === 'high' ? 'warning' : (priority === 'medium' ? 'info' : 'info');
      latest.className = 'agent-latest-alert ' + levelClass;
      latest.innerHTML = `<strong>综合驾驶建议</strong>${this.escHtml(text)}${advice.signals_summary ? '<br><em>信号：' + this.escHtml(advice.signals_summary) + '</em>' : ''}<br><span class="agent-latest-hint">多路感知融合 · 点击查看告警中心</span>`;
      latest.classList.remove('hidden');
      latest.onclick = () => {
        const panel = document.getElementById('scenario-fusion-panel');
        if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      };
      latest.title = '查看多路感知融合建议';
    }

    const subtitle = document.getElementById('agent-subtitle');
    if (subtitle) subtitle.textContent = '刚生成：综合驾驶建议';
  },

  async loadScenarioFusion(opts = {}) {
    try {
      const [snapshot, conflicts, advice] = await Promise.all([
        this.api('/api/scenario/snapshot'),
        this.api('/api/scenario/conflicts?limit=10'),
        this.api('/api/scenario/advice'),
      ]);
      this.renderScenarioSnapshot(snapshot);
      this.renderScenarioDrivingAdvice(advice);
      this.notifyAgentDrivingAdvice(advice, { force: opts.fromAlert || advice.cached === false });
      this.renderScenarioConflicts(conflicts.items || []);
    } catch (e) {
      const snapEl = document.getElementById('scenario-snapshot');
      if (snapEl) snapEl.innerHTML = `<p class="hint">多路感知数据加载失败：${this.escHtml(e.message)}</p>`;
    }
  },

  renderScenarioSnapshot(snapshot) {
    const el = document.getElementById('scenario-snapshot');
    if (!el || !snapshot) return;
    const lpr = snapshot.lpr || {};
    const police = snapshot.police || {};
    const owner = snapshot.owner || {};
    const plates = this.formatPlateLabels(lpr.plates).slice(0, 3).join('、') || '—';
    const ownerMain = owner.action_cn || owner.action || owner.gesture_cn || owner.gesture || '—';
    const ownerSub = owner.action
      ? (owner.gesture_cn || owner.gesture || '—')
      : (owner.gesture_cn || owner.gesture ? '未触发控车动作' : '—');
    const lprSource = this.recognitionMirrorSource('lpr', lpr.source);
    const policeSource = this.recognitionMirrorSource('police', police.source);
    const ownerSource = this.recognitionMirrorSource('owner', owner.source);
    const suppressed = snapshot.owner_suppressed
      ? `<span class="scenario-badge warning">车主动作已抑制</span>`
      : '';
    el.innerHTML = `
      <div class="scenario-signal-card">
        <span class="scenario-signal-label">车牌</span>
        <strong>${lpr.plate_count || 0} 个</strong>
        <small>${this.escHtml(plates)}${lprSource ? ` · ${this.escHtml(lprSource)}` : ''}</small>
      </div>
      <div class="scenario-signal-card">
        <span class="scenario-signal-label">交警</span>
        <strong>${this.escHtml(police.gesture_cn || police.gesture || '—')}</strong>
        <small>${police.confidence != null ? (police.confidence * 100).toFixed(0) + '%' : '—'}${policeSource ? ` · ${this.escHtml(policeSource)}` : ''}</small>
      </div>
      <div class="scenario-signal-card">
        <span class="scenario-signal-label">车主</span>
        <strong>${this.escHtml(ownerMain)}</strong>
        <small>${this.escHtml(ownerSub)}${ownerSource ? ` · ${this.escHtml(ownerSource)}` : ''}</small>
      </div>
      <div class="scenario-signal-card scenario-meta-card">
        <span class="scenario-signal-label">窗口</span>
        <strong>${snapshot.window_seconds || 30}s</strong>
        <small>未处理冲突 ${snapshot.open_conflicts || 0} 条 ${suppressed}</small>
      </div>`;
    this._lastScenarioSnapshot = snapshot;
  },

  renderScenarioDrivingAdvice(advice) {
    const el = document.getElementById('scenario-driving-advice');
    if (!el || !advice) return;
    const modeLabel = advice.mode === 'llm' ? 'LLM 融合推理' : '规则模板';
    const priority = ['high', 'medium', 'normal'].includes(advice.priority) ? advice.priority : 'normal';
    const signals = advice.signals_summary || '—';
    el.innerHTML = `
      <div class="scenario-advice-card ${priority}">
        <div class="scenario-advice-head">
          <strong>🧭 综合驾驶建议</strong>
          <span class="scenario-badge ${priority === 'high' ? 'critical' : priority}">${this.escHtml(modeLabel)}</span>
        </div>
        <p class="scenario-advice-text">${this.escHtml(advice.advice || '暂无建议')}</p>
        <small class="scenario-advice-meta">信号：${this.escHtml(signals)}</small>
      </div>`;
  },

  formatPlateLabels(plates) {
    if (!Array.isArray(plates)) return [];
    return plates.map(p => {
      if (typeof p === 'string') return p.trim();
      if (p && typeof p === 'object') {
        return String(p.plate_number || p.plate || p.text || p.number || '').trim();
      }
      return '';
    }).filter(Boolean);
  },

  renderScenarioConflicts(items) {
    const el = document.getElementById('scenario-conflicts');
    if (!el) return;
    const hint = (this._lastScenarioSnapshot && this._lastScenarioSnapshot.fusion_status_hint) || '';
    if (!items.length) {
      el.innerHTML = `<p class="hint">${this.escHtml(hint || '近期无场景冲突，三路感知信号一致。')}</p>`;
      return;
    }
    el.innerHTML = items.map(c => {
      const status = c.status === 'open' ? '未处理' : '已处理';
      const sev = ['critical', 'warning', 'info'].includes(c.severity) ? c.severity : 'warning';
      const conflictId = Number(c.id);
      const alertId = Number(c.alert_id);
      const resolveBtn = c.status === 'open'
        && Number.isSafeInteger(conflictId)
        ? `<button class="btn small" onclick="App.resolveScenarioConflict(${conflictId})">确认处置</button>`
        : '';
      const alertLink = Number.isSafeInteger(alertId) && alertId > 0
        ? `<button class="btn small" onclick="App.viewReplay(${alertId})">查看告警</button>`
        : '';
      return `<div class="scenario-conflict-card ${sev}">
        <div class="scenario-conflict-head">
          <strong>${this.escHtml(c.conflict_type || '场景冲突')}</strong>
          <span class="scenario-badge ${sev}">${this.escHtml(status)}</span>
        </div>
        <p class="scenario-fusion-text">💡 ${this.escHtml(c.fusion_recommendation || '')}</p>
        <div class="scenario-conflict-actions">${resolveBtn}${alertLink}</div>
      </div>`;
    }).join('');
  },

  async resolveScenarioConflict(conflictId) {
    try {
      await this.api(`/api/scenario/conflicts/${conflictId}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ resolution_note: '已按融合建议处置' }),
      });
      await this.loadScenarioFusion();
      await this.loadAlerts();
      this.showAgentSpeech('场景冲突已确认处置，车主动作抑制已解除', 5000);
    } catch (e) {
      alert(e.message);
    }
  },
  });
})();
