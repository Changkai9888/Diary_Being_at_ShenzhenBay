(() => {
  'use strict';
  // Preview data under shell.v1 is deliberately not read, modified, or uploaded.
  const KEY = 'szb.cognitive-workspace.client.v1';
  const mount = new URL('./', window.location.href);
  const apiURL = (path) => new URL(path.replace(/^\//, ''), mount);
  let storageKey = null, sessionTimer, sessionCheck, sessionInvalid = false;
  const emptyClient = () => ({ version: 1, currentId: null, archivedView: false, sidebarCollapsed: false, drafts: {} });
  const $ = (id) => document.getElementById(id);
  const shell = $('appShell');
  const mobile = matchMedia('(max-width: 900px)');
  const modelNames = { auto: '自动选择', flash: 'DeepSeek4 Flash', pro: 'DeepSeek4 Pro' };
  const chats = new Map(), mutations = new Set(), revisions = new Map(), returnFocus = new Map();
  let storageOK = true, storageError = '', saveTimer, toastTimer, pollTimer, refreshPromise;
  let refreshAgain = false, epoch = 0, loading = true, connectionError = '', renderedId;
  let dialogMode = 'create', dialogTarget, composing = false, oldOverflow = '', profileSaving = false;
  function loadClient() {
    const empty = emptyClient();
    try {
      const raw = localStorage.getItem(storageKey);
      if (!raw) return empty;
      const data = JSON.parse(raw);
      if (!data || data.version !== 1 || !data.drafts || typeof data.drafts !== 'object' || Array.isArray(data.drafts)) throw Error('Invalid data');
      return { version: 1, currentId: typeof data.currentId === 'string' ? data.currentId : null,
        archivedView: data.archivedView === true, sidebarCollapsed: data.sidebarCollapsed === true,
        drafts: Object.fromEntries(Object.entries(data.drafts).filter(([, value]) => typeof value === 'string')) };
    } catch {
      storageOK = false;
      storageError = '浏览器草稿无法读取，原数据已保留；本次草稿仅在当前页面保留。';
      return empty;
    }
  }
  const client = emptyClient();
  const state = { workspaces: [], profile: { notes: '', learned: [], version: 0 }, online: false, configured: false, csrf: '', busy: new Set(), memory: { pending: [], errors: {} }, budget: null, userId: null, gateway: false, loginURL: null, sessionStatusURL: null, diaryURL: null };
  const workspaceById = (id) => state.workspaces.find((w) => w.id === id);
  const current = () => workspaceById(client.currentId);
  const isGlobal = (w) => w?.kind === 'global';
  const globalWorkspace = () => state.workspaces.find(isGlobal);
  const firstWorkspace = () => (client.archivedView ? null : globalWorkspace())
    || state.workspaces.find((w) => !isGlobal(w) && Boolean(w.archived) === client.archivedView);
  const draft = (id) => id && Object.hasOwn(client.drafts, id) ? client.drafts[id] : '';
  const busy = (id) => state.busy.has(id) || Boolean(chats.get(id)?.active);
  function uuid() {
    return crypto.randomUUID?.() || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const n = Math.floor(Math.random() * 16); return (c === 'x' ? n : (n & 3) | 8).toString(16);
    });
  }
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function icon(name = 'chat') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#icon-' + name); svg.append(use); return svg;
  }
  function toast(text) {
    clearTimeout(toastTimer); $('toast').textContent = text; $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6500);
  }
  function saveClient() {
    clearTimeout(saveTimer); if (!storageOK || !storageKey || sessionInvalid) return;
    try { localStorage.setItem(storageKey, JSON.stringify(client)); }
    catch {
      storageOK = false; storageError = '浏览器草稿保存失败；对话由后台保存，未发送草稿仅在当前页面保留。';
      chrome(); toast(storageError);
    }
  }
  function setDraft(id, text, immediate = false) {
    client.drafts[id] = text; revisions.set(id, (revisions.get(id) || 0) + 1);
    clearTimeout(saveTimer); if (immediate) saveClient(); else saveTimer = setTimeout(saveClient, 250);
  }
  function safeLocalPath(value) {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return null;
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin ? url.pathname + url.search : null;
  }
  function updateDiaryReturn() {
    const link = $('diaryReturn');
    if (state.diaryURL) {
      link.href = state.diaryURL;
      link.setAttribute('aria-label', '返回日记本'); link.title = '返回日记本';
    } else {
      link.removeAttribute('href'); link.removeAttribute('title');
      link.setAttribute('aria-label', '认知工作区');
    }
  }
  function clearSessionView() {
    sessionInvalid = true; clearTimeout(saveTimer); clearTimeout(pollTimer); clearInterval(sessionTimer);
    storageKey = null; epoch++;
    for (const chat of chats.values()) chat.controller?.abort();
    chats.clear(); mutations.clear(); revisions.clear();
    Object.assign(client, emptyClient());
    Object.assign(state, { workspaces: [], profile: { notes: '', learned: [], version: 0 }, online: false,
      configured: false, csrf: '', busy: new Set(), memory: { pending: [], errors: {} }, budget: null, diaryURL: null });
    updateDiaryReturn();
    $('messageInput').value = ''; $('profileNotes').value = ''; $('workspaceSearch').value = '';
    for (const id of ['profileDialog', 'workspaceDialog']) if ($(id).open) $(id).close();
    render(); renderLearned();
  }
  function expireSession(redirect) {
    const inferred = mount.pathname.endsWith('/cognitive/') ? mount.pathname.slice(0, -'cognitive/'.length) + 'login' : null;
    const target = safeLocalPath(redirect) || state.loginURL || inferred;
    connectionError = '日记登录已失效，正在返回登录页面。'; clearSessionView();
    if (target) window.location.assign(target);
  }
  function changedSession() {
    connectionError = '日记登录用户已切换，正在重新载入对应工作区。'; clearSessionView();
    window.location.reload();
  }
  function configureClient(data) {
    if (!/^user(?:0|[1-9][0-9]{0,8})$/.test(data.user_id)) throw Error('后台用户身份不合法。');
    if (state.userId !== null && state.userId !== data.user_id) { changedSession(); return false; }
    if (state.userId === null) {
      state.gateway = data.gateway_mode === true; state.userId = data.user_id;
      storageKey = state.gateway ? KEY + ':' + encodeURIComponent(mount.pathname) + ':' + data.user_id : KEY;
      Object.assign(client, loadClient()); sidebarMode(); if (storageError) toast(storageError);
    }
    if (state.gateway) {
      state.loginURL = safeLocalPath(data.login_url); state.sessionStatusURL = safeLocalPath(data.session_status_url);
      const diaryURL = safeLocalPath(data.diary_url);
      if (!state.loginURL || !state.sessionStatusURL || !diaryURL) throw Error('日记网关返回路径不完整。');
      state.diaryURL = diaryURL;
      if (!sessionTimer) sessionTimer = setInterval(() => { void checkSession(); }, 15000);
    } else {
      state.diaryURL = null;
    }
    updateDiaryReturn();
    return true;
  }
  async function checkSession() {
    if (sessionInvalid) return false;
    if (!state.gateway) return true;
    if (sessionCheck) return sessionCheck;
    sessionCheck = (async () => {
      try {
        const response = await fetch(state.sessionStatusURL, { credentials: 'same-origin', cache: 'no-store',
          headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(10000) });
        if (!response.ok) throw await httpError(response);
        const data = await response.json();
        if (data.user_id !== state.userId) { changedSession(); return false; }
        if (!state.online && !sessionInvalid) void refresh(true);
        return true;
      } catch (error) {
        if (!sessionInvalid) {
          state.online = false; connectionError = '暂时无法验证日记登录状态，请重新连接。'; chrome();
        }
        return false;
      } finally { sessionCheck = null; }
    })();
    return sessionCheck;
  }
  async function httpError(response) {
    let data; try { data = await response.json(); } catch { data = {}; }
    const error = Error(typeof data.error === 'string' ? data.error : data.error?.message || data.message || '服务器返回 HTTP ' + response.status + '，操作未完成。');
    error.status = response.status;
    error.retryable = response.status !== 401 && (typeof data.retryable === 'boolean' ? data.retryable : response.status >= 500 || [409, 429].includes(response.status));
    if (response.status === 401) { expireSession(data.redirect); }
    return error;
  }
  async function api(path, method = 'GET', body) {
    if (sessionInvalid) throw Error('日记登录已失效。');
    const headers = { Accept: 'application/json' };
    if (method !== 'GET') {
      if (!(await checkSession())) throw Error('日记登录状态尚未验证，请重新连接。');
      if (!state.online || !state.csrf) throw Error('后台尚未连接，当前不能保存修改。');
      headers['Content-Type'] = 'application/json'; headers['X-Workspace-Token'] = state.csrf;
    }
    let response;
    try { response = await fetch(apiURL(path), { method, headers, credentials: 'same-origin', cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) }); }
    catch {
      state.online = false;
      connectionError = '无法连接后台。请运行 cognitive_workspace/server.py，并通过它提供的本地地址打开页面。';
      chrome();
      throw Error(connectionError);
    }
    if (!response.ok) throw await httpError(response);
    try { return await response.json(); } catch { throw Error('后台返回格式异常，请检查服务地址后重新连接。'); }
  }
  function report(error) { toast(error?.message || '操作未完成，请重试。'); }
  function onAsync(id, type, action) {
    $(id).addEventListener(type, (event) => {
      if (type === 'submit') event.preventDefault();
      Promise.resolve().then(() => action(event)).catch(report);
    });
  }
  function chooseCurrent() {
    if (!current() || !isGlobal(current()) && Boolean(current().archived) !== client.archivedView)
      client.currentId = firstWorkspace()?.id || null;
  }
  function poll() {
    clearTimeout(pollTimer);
    if (state.busy.size || state.memory.pending.length || [...chats.values()].some((chat) => chat.active))
      pollTimer = setTimeout(() => { void refresh(true); }, 5000);
  }
  function refresh(silent = false) {
    if (sessionInvalid) return Promise.resolve();
    if (refreshPromise) { refreshAgain = true; return refreshPromise; }
    const requestEpoch = epoch;
    refreshPromise = (async () => {
      try {
        const data = await api('/api/state');
        if (!data || !Array.isArray(data.workspaces) || typeof data.csrf_token !== 'string'
          || !data.workspaces.every((w) => w && typeof w.id === 'string' && typeof w.name === 'string' && Array.isArray(w.messages)))
          throw Error('后台状态格式不完整，请检查服务版本。');
        if (sessionInvalid) return;
        if (requestEpoch !== epoch) { refreshAgain = true; return; }
        if (!configureClient(data)) return;
        state.online = true; state.configured = data.api_configured === true; state.csrf = data.csrf_token;
        state.workspaces = data.workspaces; state.profile = data.profile || { notes: '', learned: [], version: 0 };
        state.busy = new Set(Array.isArray(data.busy_workspaces) ? data.busy_workspaces : []);
        state.memory = { pending: Array.isArray(data.memory_status?.pending) ? data.memory_status.pending : [],
          errors: data.memory_status?.errors && typeof data.memory_status.errors === 'object' ? data.memory_status.errors : {} };
        state.budget = data.budget || null;
        connectionError = '';
        for (const [id, chat] of chats) {
          if (!chat.active && workspaceById(id)?.messages.some((m) => m.role === 'assistant' && m.request_id === chat.requestId && m.status === 'complete')) chats.delete(id);
        }
        chooseCurrent(); saveClient(); render(); renderLearned();
      } catch (error) {
        state.online = false; connectionError = error.message; render(); if (!silent) toast(connectionError);
      } finally {
        loading = false; refreshPromise = null; chrome(); if (!sessionInvalid) poll();
        if (refreshAgain && !sessionInvalid) { refreshAgain = false; setTimeout(() => { void refresh(true); }, 0); }
      }
    })();
    return refreshPromise;
  }
  function upsertWorkspace(w) {
    if (!w || typeof w.id !== 'string' || !Array.isArray(w.messages)) throw Error('后台没有返回有效工作区。');
    const index = state.workspaces.findIndex((item) => item.id === w.id);
    if (index < 0) state.workspaces.unshift(w); else state.workspaces[index] = w;
  }
  async function patch(id, changes) {
    if (mutations.has(id)) return null;
    mutations.add(id); epoch++; chrome();
    try { const w = await api('/api/workspaces/' + encodeURIComponent(id), 'PATCH', changes); epoch++; upsertWorkspace(w); return w; }
    finally { mutations.delete(id); renderList(); chrome(); }
  }
  function upsertMessage(id, message) {
    const w = workspaceById(id);
    if (!w || !message || typeof message.text !== 'string') return;
    const index = w.messages.findIndex((m) => m.id === message.id);
    if (index < 0) w.messages.push(message); else w.messages[index] = message;
  }
  function renderList() {
    const query = $('workspaceSearch').value.trim().toLocaleLowerCase();
    const global = globalWorkspace();
    const available = state.workspaces.filter((w) => !isGlobal(w) && Boolean(w.archived) === client.archivedView);
    const filtered = available.filter((w) => (w.name + ' ' + (w.purpose || '')).toLocaleLowerCase().includes(query));
    $('listLabel').textContent = client.archivedView ? '已归档工作区' : '我的工作区';
    $('workspaceCount').textContent = available.length;
    $('archiveLabel').textContent = client.archivedView ? '返回工作区' : '已归档';
    $('archiveFilter').setAttribute('aria-pressed', String(client.archivedView));
    const fragment = document.createDocumentFragment(), pinned = document.createDocumentFragment();
    for (const w of [...(global ? [global] : []), ...filtered]) {
      const globalEntry = isGlobal(w);
      const button = el('button', 'workspace-item' + (globalEntry ? ' workspace-global' : '')); button.type = 'button';
      if (w.id === client.currentId) button.setAttribute('aria-current', 'page');
      const mark = el('span', 'workspace-icon'); mark.append(icon(globalEntry ? 'compass' : 'chat'));
      const copy = el('span', 'workspace-copy');
      const heading = el('span', 'workspace-name-line'); heading.append(el('span', 'workspace-name', w.name));
      if (globalEntry) heading.append(el('span', 'global-badge', '常驻'));
      copy.append(heading, el('span', 'workspace-preview',
        draft(w.id) ? '草稿：' + draft(w.id) : w.messages.at(-1)?.text || w.purpose || (globalEntry ? '生活、关系与长期方向' : '从一个想法开始')));
      button.append(mark, copy, el('span', 'workspace-time', busy(w.id) ? '回复中' : w.archived ? '已归档' : draft(w.id) ? '草稿' : ''));
      button.addEventListener('click', () => { client.currentId = w.id; closeMenu(); saveClient(); render(); closeSidebar(); });
      (globalEntry ? pinned : fragment).append(button);
    }
    if (!filtered.length) fragment.append(el('p', 'workspace-list-empty', query ? '没有找到相关工作区' : client.archivedView ? '还没有归档的工作区' : '新建一个工作区，开始持续讨论'));
    $('globalWorkspace').replaceChildren(pinned); $('globalWorkspace').hidden = !global;
    $('workspaceList').replaceChildren(fragment);
  }
  function metricsText(message) {
    const m = message.metrics || {}, parts = [];
    if (typeof m.model === 'string') parts.push(m.model);
    if (Number.isFinite(m.first_token_ms)) parts.push('首字 ' + (m.first_token_ms / 1000).toFixed(1) + ' 秒');
    if (Number.isFinite(m.elapsed_ms)) parts.push('总计 ' + (m.elapsed_ms / 1000).toFixed(1) + ' 秒');
    if (Number.isFinite(m.estimated_cost_cny_upper)) parts.push('费用上限估计 ¥' + m.estimated_cost_cny_upper.toFixed(4));
    return parts.join(' · ');
  }
  function retryContext(w, message) {
    const user = w.messages.find((m) => m.role === 'user' && m.request_id === message.request_id);
    if (!user || !message.request_id) return null;
    const model = message.metrics?.requested_model;
    return { requestId: message.request_id, content: user.text, model: Object.hasOwn(modelNames, model) ? model : w.model || 'auto',
      text: message.text || '', error: message.metrics?.error || '上次回复未完成。', retryable: true, userMessage: user };
  }
  function renderMessages(w, bottom = false) {
    const scroll = $('conversationScroll'), oldTop = scroll.scrollTop;
    const follow = bottom || scroll.scrollHeight - oldTop - scroll.clientHeight < 90;
    const chat = w ? chats.get(w.id) : null;
    let messages = w ? [...w.messages] : [];
    if (chat) {
      messages = messages.filter((m) => !(m.role === 'assistant' && m.request_id === chat.requestId));
      if (!messages.some((m) => m.role === 'user' && m.request_id === chat.requestId)) messages.push(chat.userMessage);
      messages.push({ id: 'pending-' + chat.requestId, role: 'assistant', text: chat.text, status: chat.active ? 'streaming' : 'failed',
        request_id: chat.requestId, metrics: { error: chat.error }, local: true });
    }
    const complete = new Set(messages.filter((m) => m.role === 'assistant' && m.status === 'complete').map((m) => m.request_id));
    const failures = new Map();
    for (const m of messages) if (m.role === 'assistant' && m.status === 'failed') failures.set(m.request_id, m.id);
    const fragment = document.createDocumentFragment();
    for (const message of messages) {
      if (!message || !['user', 'assistant'].includes(message.role)) continue;
      const article = el('article', 'message ' + message.role), avatar = el('div', 'message-avatar');
      avatar.setAttribute('aria-hidden', 'true');
      if (message.role === 'user') avatar.textContent = '我'; else avatar.append(icon());
      const content = el('div', 'message-content'), meta = el('div', 'message-meta'), bubble = el('div', 'message-bubble');
      meta.append(el('span', 'message-author', message.role === 'user' ? '我' : '思考伙伴'));
      if (message.status === 'streaming') meta.append(el('span', 'message-badge', '回复中'));
      if (message.status === 'failed') meta.append(el('span', 'message-badge', '未完成'));
      const text = message.text || (message.status === 'streaming' ? chat?.phase || '后台正在处理，请稍候…' : '这次回复未能完成。');
      for (const paragraph of text.split(/\n{2,}/)) { const p = el('p', '', paragraph); p.style.whiteSpace = 'pre-wrap'; bubble.append(p); }
      const footnote = el('div', 'message-footnote', message.role === 'user' ? message.localPending ? '等待后台确认' : '已保存到本地后台' : metricsText(message));
      if (message.status === 'failed') {
        footnote.append(el('span', 'message-error', (message.local ? chat?.error : message.metrics?.error) || '回复中断，已有内容仍保留。'));
        if (!complete.has(message.request_id) && failures.get(message.request_id) === message.id) {
          const context = message.local ? chat : retryContext(w, message);
          if (context && context.retryable !== false) {
            const retry = el('button', 'text-button retry-button', '重试这次回复'); retry.type = 'button';
            retry.disabled = !state.online || !state.configured || w.archived || busy(w.id);
            retry.addEventListener('click', () => { void sendChat(w.id, context.content, context.model, context).catch(report); });
            footnote.append(retry);
          }
        }
      } else if (message.status === 'streaming') footnote.textContent = chat?.phase || '后台仍在处理，切换工作区不会打断回复';
      content.append(meta, bubble, footnote); article.append(avatar, content); fragment.append(article);
    }
    $('messages').replaceChildren(fragment); $('messages').hidden = !messages.length; $('emptyState').hidden = Boolean(messages.length);
    $('emptyState').querySelector('.eyebrow').textContent = isGlobal(w) ? '一个常驻的空间，陪你看见全局' : '留一个空间，持续想清楚';
    $('emptyState').querySelector('h2').textContent = isGlobal(w) ? '从你此刻在意的事说起' : w ? '从一个问题开始' : client.archivedView ? '还没有归档的工作区' : '先为一段讨论，留一个位置';
    $('emptyState').querySelector('.empty-description').textContent = isGlobal(w)
      ? '个人生活、家庭、情绪、长期方向，或商业问题，都可以在这里聊。需要时，再结合其他工作区的背景，一起理清轻重缓急。'
      : w ? '补充背景、交换想法，让一个值得持续思考的问题一步步向前。' : '点击「新建工作区」，写下名称和讨论目的，再开始真实对话。';
    $('emptyCreate').hidden = Boolean(w) || client.archivedView;
    for (const starter of document.querySelectorAll('[data-starter]')) {
      starter.disabled = !w || w.archived || !state.online;
      starter.querySelector('span').textContent = isGlobal(w) ? starter.dataset.globalLabel : starter.dataset.label;
    }
    requestAnimationFrame(() => { if ((w?.id || null) === (current()?.id || null)) scroll.scrollTop = follow ? scroll.scrollHeight : oldTop; });
  }
  function chrome() {
    const w = current(), active = Boolean(w && busy(w.id)), locked = !state.online || !w || w.archived || mutations.has(w?.id);
    $('newWorkspace').disabled = !state.online; $('emptyCreate').disabled = !state.online;
    $('localStatus').textContent = loading ? '正在连接本地后台' : !state.online ? '后台未连接' : !state.configured ? '后台已连接 · API 未配置' : '本地后台已连接';
    $('localStatus').classList.toggle('storage-warning', !state.online || !state.configured);
    $('localStatus').title = connectionError || '对话由本地后台保存；浏览器只保存未发送草稿和界面偏好。';
    const budget = state.budget;
    $('budgetStatus').hidden = !budget || !Number.isFinite(budget.accounted_cny_upper);
    $('budgetStatus').textContent = budget && Number.isFinite(budget.accounted_cny_upper)
      ? '已记费用上限 ¥' + budget.accounted_cny_upper.toFixed(3)
        + (Number.isFinite(budget.max_cost_cny) ? ' / ¥' + budget.max_cost_cny.toFixed(2) : '') : '';
    $('budgetStatus').title = budget && Number.isFinite(budget.calls) ? '已记录 ' + budget.calls + ' 次模型调用，包含后台整理。' : '费用为已记录调用的保守上限估计。';
    const memoryError = w && Object.hasOwn(state.memory.errors, w.id) ? state.memory.errors[w.id] : '';
    const memoryPending = Boolean(w && state.memory.pending.includes(w.id));
    $('memoryNotice').hidden = !memoryError && !memoryPending;
    $('memoryNotice').classList.toggle('memory-warning', Boolean(memoryError));
    $('memoryNotice').textContent = memoryError
      ? '回复已保存；记忆整理未完成：' + String(memoryError)
      : memoryPending ? '回复已保存，正在整理本次讨论的记忆。' : '';
    $('connectionBanner').hidden = state.online && state.configured;
    $('connectionMessage').textContent = loading ? '正在连接本地后台…' : !state.online ? connectionError || '后台未连接。请运行 cognitive_workspace/server.py 后，通过本地服务地址打开页面。' : 'DeepSeek API 尚未配置。工作区可管理，配置后才能发送对话。';
    $('reconnectButton').disabled = loading || Boolean(refreshPromise); $('reconnectButton').textContent = state.online ? '重新检查' : '重新连接';
    $('workspaceSubline').textContent = !w ? '从一个值得持续思考的问题开始' : w.archived ? '已归档 · 历史由本地后台保存' : active ? '正在回复 · 可以切换工作区继续查看' : !state.online ? '后台连接中断 · 当前为只读状态' : isGlobal(w) ? '长期陪伴 · 按需关联其他工作区' : '独立话题 · 对话由本地后台保存';
    for (const id of ['infoToggle', 'moreToggle']) $(id).disabled = !w;
    for (const id of ['editGoal', 'infoName', 'infoPurpose', 'saveInfoButton', 'renameWorkspace']) $(id).disabled = locked || active;
    $('archiveWorkspace').hidden = isGlobal(w);
    $('archiveWorkspace').disabled = !state.online || !w || isGlobal(w) || active || mutations.has(w?.id); $('restoreWorkspace').disabled = !state.online || active;
    $('messageInput').disabled = locked; $('modelSelect').disabled = locked || active;
    $('sendButton').disabled = locked || active || !state.configured || !$('messageInput').value.trim();
    $('sendButton').title = active ? '正在回复，可以先写下一条草稿' : '发送给思考伙伴';
    $('profileNotes').disabled = !state.online || profileSaving;
    $('profileForm').querySelector('[type="submit"]').disabled = !state.online || profileSaving;
    $('profileStatus').textContent = !state.online ? '后台未连接，备注暂时只读。' : profileSaving ? '正在保存备注…' : '手动备注由后台保存 · 共享认知版本 ' + (state.profile.version ?? 0) + '，可以通过对话纠正自动记录'
      + (Object.keys(state.memory.errors).length ? ' · 有工作区的记忆整理未完成，请返回该工作区查看提示。' : '');
    $('draftNotice').textContent = !state.online ? '后台未连接，暂时不能发送或保存修改' : !w ? '新建一个工作区后开始讨论'
      : w.archived ? '此工作区已归档，恢复后可以继续讨论' : active ? (chats.get(w.id)?.phase || '后台正在处理') + ' · 可以先写下一条草稿'
        : !storageOK ? storageError : !state.configured ? 'API 未配置 · 未发送草稿保存在当前浏览器'
          : draft(w.id) ? '未发送草稿自动保存在当前浏览器' : 'Enter 发送 · Shift + Enter 换行';
  }
  function resizeInput() {
    const input = $('messageInput'); input.style.height = 'auto';
    input.style.height = Math.min(Math.max(input.scrollHeight, 52), 180) + 'px';
    input.style.overflowY = input.scrollHeight > 180 ? 'auto' : 'hidden';
  }
  function fillInfo(w) { $('infoName').value = w?.name || ''; $('infoName').setCustomValidity(''); $('infoPurpose').value = w?.purpose || ''; }
  function render() {
    const w = current(), changed = renderedId !== (w?.id || null); renderedId = w?.id || null;
    shell.classList.toggle('global-workspace', isGlobal(w));
    document.querySelector('.preview-badge').textContent = isGlobal(w) ? '常驻统筹' : '本地工作区';
    renderList(); $('workspaceTitle').textContent = w?.name || (client.archivedView ? '已归档工作区' : '你的思考工作区');
    $('goalText').textContent = w?.purpose || (isGlobal(w) ? '从生活到长期方向，按需串联各个讨论' : '为这个工作区写下一个讨论目的');
    $('infoForm').querySelector('.field-help').textContent = isGlobal(w)
      ? '这是常驻的全局讨论空间，不会归档。名称和目的都可以调整，生活、家庭与事业都可以聊。'
      : '目的可以随着讨论改变。这里先保留你当前的定义。';
    if (changed || $('infoPanel').hidden) fillInfo(w);
    $('archiveWorkspace').textContent = w?.archived ? '恢复工作区' : '归档工作区'; $('archiveBanner').hidden = !w?.archived;
    if ($('messageInput').value !== draft(w?.id)) $('messageInput').value = draft(w?.id);
    $('modelSelect').querySelector('[value="auto"]').textContent = isGlobal(w) ? '自动（统筹优先 Pro）' : modelNames.auto;
    $('modelSelect').value = w?.model || 'auto'; renderMessages(w, changed); chrome(); requestAnimationFrame(resizeInput);
  }
  function renderLearned() {
    const fragment = document.createDocumentFragment(), items = Array.isArray(state.profile.learned) ? state.profile.learned : [];
    if (!items.length) fragment.append(el('p', 'field-help', '还没有自动认知记录。以后可在这里查看记录及其来源，并在对话中纠正。'));
    for (const item of items) {
      const row = el('article', 'learned-item'), heading = el('div', 'learned-heading');
      heading.append(el('span', 'message-badge', item.kind === 'hypothesis' ? '待确认推测' : '明确表达'));
      if (item.key) heading.append(el('span', '', item.key));
      row.append(heading, el('p', '', item.text || ''));
      const w = workspaceById(item.source_workspace_id), sources = el('div', 'learned-sources', '来源：' + (w?.name || '其他工作区'));
      for (const id of Array.isArray(item.source_ids) ? item.source_ids : []) {
        const reference = el('span', 'source-reference', '记录 ' + String(id).slice(0, 8));
        reference.title = w?.messages.find((m) => m.id === id)?.text || '消息 ID：' + id; sources.append(reference);
      }
      row.append(sources); fragment.append(row);
    }
    $('profileLearned').replaceChildren(fragment);
  }
  function renderChat(id, bottom = false) { renderList(); if (client.currentId === id) renderMessages(workspaceById(id), bottom); chrome(); }
  async function sendChat(id, content, model, previous = null) {
    const w = workspaceById(id);
    if (!w || w.archived || !state.online || !state.configured || busy(id)) return;
    if (!(await checkSession()) || sessionInvalid) return;
    const requestId = previous?.requestId || uuid();
    const controller = new AbortController();
    const chat = { ...previous, controller, requestId, content, model, active: true, text: '', error: '', retryable: true, phase: '正在提交问题', started: false,
      userMessage: previous?.userMessage || { id: 'local-' + requestId, role: 'user', text: content, request_id: requestId, localPending: true } };
    chats.set(id, chat); epoch++;
    if (!previous) setDraft(id, '', true);
    const clearedRevision = revisions.get(id) || 0;
    if (client.currentId === id && !previous) { $('messageInput').value = ''; resizeInput(); }
    renderChat(id, true); poll();
    let terminal = false, reader;
    try {
      const response = await fetch(apiURL('/api/chat'), { method: 'POST', signal: controller.signal, credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json', 'X-Workspace-Token': state.csrf },
        body: JSON.stringify({ workspace_id: id, content, model, request_id: requestId, ...(previous ? { retry: true } : {}) }) });
      if (!response.ok) throw await httpError(response);
      if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw Error('后台没有返回有效的回复流。');
      reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      function dispatch(record) {
        let type = 'message'; const lines = [];
        for (const line of record.split(/\r?\n/)) {
          if (line.startsWith('event:')) type = line.slice(6).trim();
          else if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
        }
        if (!lines.length) return;
        let data; try { data = JSON.parse(lines.join('\n')); } catch { throw Error('回复流格式异常，请保留当前内容后重试。'); }
        if (data.workspace_id && data.workspace_id !== id) throw Error('回复工作区不匹配，已停止展示这段数据。');
        if (data.request_id && data.request_id !== requestId) throw Error('回复请求不匹配，已停止展示这段数据。');
        if (type === 'start') {
          chat.started = true; chat.phase = typeof data.phase === 'string' ? data.phase : '正在思考';
          if (data.user_message) { chat.userMessage = data.user_message; upsertMessage(id, data.user_message); }
          state.busy.add(id);
        } else if (type === 'status') chat.phase = typeof data.message === 'string' ? data.message : '正在处理';
        else if (type === 'reset') { chat.text = typeof data.text === 'string' ? data.text : ''; chat.phase = '正在整理回复'; }
        else if (type === 'delta') { if (typeof data.text === 'string') chat.text += data.text; chat.phase = '正在回复'; }
        else if (type === 'done') {
          if (!data.message || typeof data.message.text !== 'string') throw Error('后台完成消息缺少正文。');
          upsertMessage(id, { ...data.message, metrics: data.message.metrics || data.metrics });
          terminal = true; chat.active = false; state.busy.delete(id); chats.delete(id); epoch++;
          for (const delay of [2000, 5000, 10000]) setTimeout(() => { void refresh(true); }, delay);
        } else if (type === 'error') {
          const error = Error(typeof data.message === 'string' ? data.message : '这次回复未完成。');
          error.retryable = data.retryable !== false; throw error;
        }
        renderChat(id);
      }
      while (!terminal) {
        const { value, done } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const record = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
          dispatch(record); if (terminal) break;
        }
        if (done) {
          if (!terminal && buffer.trim()) dispatch(buffer);
          if (!terminal) throw Error('连接在回复完成前中断；后台可能仍在处理，将自动检查状态。');
          break;
        }
      }
    } catch (error) {
      if (sessionInvalid) return;
      chat.active = false; chat.error = error instanceof TypeError ? '连接中断，正在检查后台状态；已有内容仍保留。' : error.message || '回复未完成，请重试。';
      chat.retryable = error.retryable !== false;
      if (!chat.started && !previous && (revisions.get(id) || 0) === clearedRevision && !draft(id)) {
        setDraft(id, content, true); if (client.currentId === id) { $('messageInput').value = content; resizeInput(); }
      }
      toast(chat.error); renderChat(id);
    } finally {
      if (reader) { try { await reader.cancel(); } catch { /* The stream may already be closed. */ } reader.releaseLock(); }
      if (!sessionInvalid) { chat.active = false; epoch++; renderChat(id); await refresh(true); }
    }
  }
  function openDialog(dialog, trigger) { returnFocus.set(dialog, trigger || document.activeElement); dialog.showModal(); }
  function closeMenu() { $('workspaceMenu').hidden = true; $('moreToggle').setAttribute('aria-expanded', 'false'); }
  function openWorkspace(mode) {
    closeMenu(); const w = current();
    if (!state.online || mode === 'rename' && (!w || w.archived || busy(w.id))) return;
    dialogMode = mode; dialogTarget = mode === 'rename' ? w.id : null;
    $('workspaceDialogTitle').textContent = mode === 'rename' ? '修改工作区' : '新建工作区';
    $('workspaceSubmit').textContent = mode === 'rename' ? '保存修改' : '创建工作区';
    $('workspaceName').value = mode === 'rename' ? w.name : ''; $('workspacePurpose').value = mode === 'rename' ? w.purpose || '' : '';
    $('workspaceName').setCustomValidity(''); openDialog($('workspaceDialog'), mode === 'rename' ? $('moreToggle') : $('newWorkspace')); $('workspaceName').focus();
  }
  function info(open) {
    $('infoPanel').hidden = !open; $('infoToggle').setAttribute('aria-expanded', String(open)); shell.classList.toggle('info-open', open);
    if (open) { fillInfo(current()); ($('infoName').disabled ? $('infoClose') : $('infoName')).focus(); }
  }
  async function archive(archived) {
    const w = current(); if (!w || isGlobal(w) || busy(w.id)) return;
    const saved = await patch(w.id, { archived }); if (!saved) return;
    if (client.currentId === w.id) {
      client.archivedView = false; client.currentId = archived ? firstWorkspace()?.id || null : w.id;
      $('workspaceSearch').value = ''; closeMenu(); info(false); saveClient(); render();
    }
    toast(archived ? '工作区已归档，可在「已归档」中恢复。' : '工作区已恢复。');
  }
  function closeSidebar() {
    if (!shell.classList.contains('sidebar-open')) return;
    shell.classList.remove('sidebar-open'); $('sidebarBackdrop').hidden = true;
    $('sidebar').removeAttribute('role'); $('sidebar').removeAttribute('aria-modal'); document.body.style.overflow = oldOverflow;
    $('menuToggle').setAttribute('aria-expanded', 'false'); $('sidebar').inert = mobile.matches || client.sidebarCollapsed;
    $('mainContent').inert = false; $('menuToggle').focus();
  }
  function sidebarMode() {
    closeSidebar(); shell.classList.toggle('sidebar-collapsed', !mobile.matches && client.sidebarCollapsed);
    $('sidebar').inert = mobile.matches || client.sidebarCollapsed; $('menuToggle').setAttribute('aria-expanded', String(!mobile.matches && !client.sidebarCollapsed));
  }

  // Add connection and memory state to the existing visual shell.
  const banner = el('div', 'connection-banner'); banner.id = 'connectionBanner'; banner.setAttribute('role', 'status');
  const bannerText = el('span'); bannerText.id = 'connectionMessage';
  const reconnect = el('button', 'text-button', '重新连接'); reconnect.id = 'reconnectButton'; reconnect.type = 'button';
  banner.append(bannerText, reconnect); document.querySelector('.conversation-area').prepend(banner);
  const memoryNotice = el('div', 'memory-notice'); memoryNotice.id = 'memoryNotice'; memoryNotice.hidden = true; memoryNotice.setAttribute('role', 'status');
  document.querySelector('.goal-strip').after(memoryNotice);
  const budgetStatus = el('div', 'budget-status'); budgetStatus.id = 'budgetStatus'; budgetStatus.hidden = true;
  $('localStatus').parentElement.after(budgetStatus);
  const create = el('button', 'button button-primary empty-create', '新建工作区'); create.id = 'emptyCreate'; create.type = 'button';
  $('emptyState').insertBefore(create, $('emptyState').querySelector('.starters'));
  const learned = el('div', 'profile-learned'); learned.id = 'profileLearned';
  $('profileStatus').after(el('h3', 'learned-title', '自动记录的共享认知'), learned);
  document.querySelector('.preview-badge').textContent = '本地工作区';
  document.querySelector('.panel-note p').textContent = '历史对话保存在本地后台。共享认知及其来源可在「关于我的认知」中查看，也可以通过对话纠正。';
  document.querySelector('.profile-explainer span:last-child').textContent = '共享认知 · 手动备注与自动记录';
  $('sendButton').setAttribute('aria-label', '发送消息');

  $('newWorkspace').addEventListener('click', () => openWorkspace('create'));
  $('emptyCreate').addEventListener('click', () => openWorkspace('create'));
  $('renameWorkspace').addEventListener('click', () => openWorkspace('rename'));
  $('workspaceCancel').addEventListener('click', () => $('workspaceDialog').close());
  $('workspaceName').addEventListener('input', () => $('workspaceName').setCustomValidity(''));
  onAsync('workspaceForm', 'submit', async () => {
    if ($('workspaceSubmit').disabled) return;
    const name = $('workspaceName').value.trim();
    if (!name) { $('workspaceName').setCustomValidity('请输入工作区名称'); $('workspaceName').reportValidity(); return; }
    const purpose = $('workspacePurpose').value.trim(), mode = dialogMode, target = dialogTarget;
    $('workspaceSubmit').disabled = true; $('workspaceCancel').disabled = true; epoch++;
    try {
      const w = mode === 'rename' ? await patch(target, { name, purpose }) : await api('/api/workspaces', 'POST', { name, purpose });
      if (!w) return;
      epoch++; upsertWorkspace(w); if (mode === 'create') { client.currentId = w.id; client.archivedView = false; }
      $('workspaceSearch').value = ''; saveClient(); $('workspaceDialog').close(); render(); closeSidebar();
      toast(mode === 'rename' ? '工作区修改已保存。' : '工作区已创建，可以开始讨论。');
    } finally { $('workspaceSubmit').disabled = false; $('workspaceCancel').disabled = false; }
  });
  $('workspaceSearch').addEventListener('input', renderList);
  $('archiveFilter').addEventListener('click', () => {
    client.archivedView = !client.archivedView; client.currentId = firstWorkspace()?.id || globalWorkspace()?.id || null;
    $('workspaceSearch').value = ''; closeMenu(); info(false); saveClient(); render();
  });
  onAsync('archiveWorkspace', 'click', () => archive(!current()?.archived));
  onAsync('restoreWorkspace', 'click', () => archive(false));
  $('moreToggle').addEventListener('click', () => {
    const open = $('workspaceMenu').hidden; $('workspaceMenu').hidden = !open; $('moreToggle').setAttribute('aria-expanded', String(open));
    if (open) $('workspaceMenu').querySelector('button:not([disabled]):not([hidden])')?.focus();
  });
  document.addEventListener('pointerdown', (event) => { if (!$('workspaceMenu').contains(event.target) && !$('moreToggle').contains(event.target)) closeMenu(); });
  $('infoToggle').addEventListener('click', () => info($('infoPanel').hidden));
  $('editGoal').addEventListener('click', () => { info(true); $('infoPurpose').focus(); });
  $('infoClose').addEventListener('click', () => { info(false); $('infoToggle').focus(); });
  $('infoName').addEventListener('input', () => $('infoName').setCustomValidity(''));
  onAsync('infoForm', 'submit', async () => {
    const w = current(); if (!w || w.archived || busy(w.id)) return;
    const name = $('infoName').value.trim();
    if (!name) { $('infoName').setCustomValidity('请输入工作区名称'); $('infoName').reportValidity(); return; }
    const saved = await patch(w.id, { name, purpose: $('infoPurpose').value.trim() }); if (!saved) return;
    if (client.currentId === w.id) { fillInfo(saved); render(); } toast('工作区信息已保存到本地后台。');
  });
  onAsync('modelSelect', 'change', async () => {
    const w = current(); if (!w || w.archived || busy(w.id)) return; const model = $('modelSelect').value;
    try { if (await patch(w.id, { model })) toast('已保存 ' + modelNames[model] + ' 偏好。'); }
    finally { if (client.currentId === w.id) $('modelSelect').value = workspaceById(w.id)?.model || 'auto'; }
  });
  $('messageInput').addEventListener('compositionstart', () => { composing = true; });
  $('messageInput').addEventListener('compositionend', () => { composing = false; });
  $('messageInput').addEventListener('input', () => {
    const w = current(); if (!w || w.archived) return; setDraft(w.id, $('messageInput').value); resizeInput(); chrome(); renderList();
  });
  $('messageInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !composing && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); if (!$('sendButton').disabled) $('composerForm').requestSubmit();
    }
  });
  onAsync('composerForm', 'submit', async () => {
    const w = current(), text = $('messageInput').value.trim();
    if (w && text && !composing && !$('sendButton').disabled) await sendChat(w.id, text, w.model || 'auto');
  });
  for (const starter of document.querySelectorAll('[data-starter]')) starter.addEventListener('click', () => {
    const w = current(); if (!w || w.archived || !state.online) return;
    setDraft(w.id, (isGlobal(w) ? starter.dataset.globalStarter : starter.dataset.starter) || starter.textContent.trim()); $('messageInput').value = draft(w.id);
    chrome(); renderList(); resizeInput(); $('messageInput').focus();
  });
  $('profileButton').addEventListener('click', () => {
    $('profileNotes').value = state.profile.notes || ''; renderLearned(); chrome();
    openDialog($('profileDialog'), $('profileButton')); $('profileNotes').focus(); void refresh(true);
  });
  $('profileCancel').addEventListener('click', () => $('profileDialog').close());
  onAsync('profileForm', 'submit', async () => {
    if (profileSaving || !state.online) return; const notes = $('profileNotes').value;
    profileSaving = true; epoch++; chrome();
    try { state.profile = await api('/api/profile', 'PUT', { notes }); epoch++; renderLearned(); toast('共享备注已保存到本地后台。'); }
    finally { profileSaving = false; chrome(); }
  });
  for (const dialog of [$('workspaceDialog'), $('profileDialog')]) {
    dialog.addEventListener('cancel', (event) => { if (dialog === $('workspaceDialog') && $('workspaceSubmit').disabled) event.preventDefault(); });
    dialog.addEventListener('close', () => {
      const target = returnFocus.get(dialog);
      if (target?.isConnected && !target.closest('[inert]') && !target.disabled) target.focus(); else $('menuToggle').focus();
    });
  }
  $('menuToggle').addEventListener('click', () => {
    if (mobile.matches) {
      if (shell.classList.contains('sidebar-open')) { closeSidebar(); return; }
      oldOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; shell.classList.add('sidebar-open');
      $('sidebar').inert = false; $('mainContent').inert = true; $('sidebarBackdrop').hidden = false;
      $('sidebar').setAttribute('role', 'dialog'); $('sidebar').setAttribute('aria-modal', 'true');
      $('menuToggle').setAttribute('aria-expanded', 'true'); $('workspaceSearch').focus();
    } else {
      client.sidebarCollapsed = !client.sidebarCollapsed; shell.classList.toggle('sidebar-collapsed', client.sidebarCollapsed);
      $('sidebar').inert = client.sidebarCollapsed; $('menuToggle').setAttribute('aria-expanded', String(!client.sidebarCollapsed)); saveClient();
    }
  });
  $('sidebarClose').addEventListener('click', closeSidebar); $('sidebarBackdrop').addEventListener('click', closeSidebar);
  mobile.addEventListener('change', sidebarMode);
  document.addEventListener('keydown', (event) => {
    if ($('workspaceDialog').open || $('profileDialog').open) return;
    if (event.key === 'Escape') {
      if (!$('workspaceMenu').hidden) { closeMenu(); $('moreToggle').focus(); }
      else if (shell.classList.contains('sidebar-open')) closeSidebar();
      else if (!$('infoPanel').hidden) { info(false); $('infoToggle').focus(); }
    }
    if (event.key === 'Tab' && shell.classList.contains('sidebar-open')) {
      const nodes = [...$('sidebar').querySelectorAll('button:not([disabled]), input:not([disabled]), a[href], [tabindex="0"]')].filter((node) => node.getClientRects().length);
      if (event.shiftKey && (document.activeElement === nodes[0] || !$('sidebar').contains(document.activeElement))) { event.preventDefault(); nodes.at(-1)?.focus(); }
      else if (!event.shiftKey && (document.activeElement === nodes.at(-1) || !$('sidebar').contains(document.activeElement))) { event.preventDefault(); nodes[0]?.focus(); }
    }
  });
  $('reconnectButton').addEventListener('click', () => { loading = true; chrome(); void refresh(); });
  window.addEventListener('pagehide', (event) => {
    saveClient();
    if (state.gateway && event.persisted) { shell.inert = true; shell.hidden = true; }
  });
  window.addEventListener('pageshow', (event) => {
    if (state.gateway && event.persisted) {
      shell.inert = true; shell.hidden = true; window.location.reload();
    }
  });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveClient(); else void checkSession().then((ok) => { if (ok) void refresh(true); }); });
  window.addEventListener('focus', () => { void checkSession(); });
  sidebarMode(); render(); if (storageError) toast(storageError); void refresh();
})();
