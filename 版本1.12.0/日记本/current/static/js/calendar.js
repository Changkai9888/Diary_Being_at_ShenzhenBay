const DIARY_BASE = document.body.dataset.diaryBase;
function diaryUrl(path) {
  return new URL(path.replace(/^\//, ''), new URL(DIARY_BASE, window.location.href)).href;
}
function diaryFetch(path, options) {
  return window.fetch(diaryUrl(path), options);
}

let currentDate = new Date();
let selectedDate = null;
let isSaving = false;
let loadedDate = null;
let logLoadRequest = 0;
let calendarLoadRequest = 0;
let assetVisible = false;
let assetRawValue = '';
let uiSoundEnabled = true;
let uiFxEnabled = true;
let audioCtx = null;
let welcomeSpokenThisLoad = false;
let welcomeRetryCount = 0;
let welcomeRetryTimer = null;
let welcomeAudio = null;

const LOG_FIELDS = [
  '天气',
  '心情',
  '醒来状态',
  '心率',
  'HP',
  'San',
  '起床体重',
  '腰围',
  '用药与健康建议',
  '计划与摘要',
  '上午',
  '下午',
  '晚上',
  '几天没做试验',
  '心得备注',
  '待提升属性',
  '在途任务'
];

// 这两个字段保存用户当前状态，不随日志日期切换。
const SHARED_FIELDS = ['待提升属性', '在途任务'];
const sharedFieldState = new Map(SHARED_FIELDS.map(field => [field, {
  initialized: false,
  dirty: false,
  revision: 0
}]));

function logField(field) {
  return document.getElementById('logForm')?.elements.namedItem(field);
}

function bindSharedFieldDrafts() {
  SHARED_FIELDS.forEach(field => {
    const element = logField(field);
    if (!element) return;
    element.addEventListener('input', () => {
      const state = sharedFieldState.get(field);
      state.initialized = true;
      state.dirty = true;
      state.revision += 1;
    });
  });
}

function initAudioCtx() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) audioCtx = new Ctx();
  }
  return audioCtx;
}

function playUiTone(kind = 'tap') {
  if (!uiSoundEnabled) return;
  const ctx = initAudioCtx();
  if (!ctx) return;
  const now = ctx.currentTime;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);

  if (kind === 'success') {
    osc.frequency.setValueAtTime(660, now);
    osc.frequency.linearRampToValueAtTime(880, now + 0.08);
  } else if (kind === 'error') {
    osc.frequency.setValueAtTime(260, now);
    osc.frequency.linearRampToValueAtTime(180, now + 0.12);
  } else {
    osc.frequency.setValueAtTime(520, now);
  }

  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.06, now + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.16);
  osc.start(now);
  osc.stop(now + 0.18);
}

function applyUiFxState() {
  document.body.classList.toggle('fx-off', !uiFxEnabled);
}

async function loadUiSettings() {
  try {
    const resp = await diaryFetch('/get_user_ui_settings');
    const data = await resp.json();
    if (!resp.ok || data.status !== 'success') return;
    const settings = data.settings || {};
    uiSoundEnabled = Boolean(settings.ui_sound_enabled);
    uiFxEnabled = Boolean(settings.ui_fx_enabled);
  } catch (_) {}
}

async function saveUiSettings() {
  try {
    await diaryFetch('/save_user_ui_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ui_sound_enabled: uiSoundEnabled,
        ui_fx_enabled: uiFxEnabled
      })
    });
  } catch (_) {}
}

function playWelcomeGreeting() {
  if (welcomeSpokenThisLoad) return;
  if (!uiSoundEnabled) return;
  try {
    if (!welcomeAudio) {
      welcomeAudio = new Audio(diaryUrl('/welcome-audio'));
      welcomeAudio.preload = 'auto';
    }
    welcomeAudio.currentTime = 0;
    const p = welcomeAudio.play();
    if (p && typeof p.then === 'function') {
      p.then(() => {
        welcomeSpokenThisLoad = true;
      }).catch(() => {});
    } else {
      welcomeSpokenThisLoad = true;
    }
  } catch (_) {}
}

function clearWelcomeRetryTimer() {
  if (welcomeRetryTimer) {
    clearInterval(welcomeRetryTimer);
    welcomeRetryTimer = null;
  }
}

function scheduleWelcomeAttempts() {
  welcomeRetryCount = 0;
  clearWelcomeRetryTimer();
  playWelcomeGreeting();
  // 语音引擎可能晚于DOMContentLoaded就绪，短时间多次尝试以实现“立即播报”
  welcomeRetryTimer = setInterval(() => {
    if (welcomeSpokenThisLoad || !uiSoundEnabled) {
      clearWelcomeRetryTimer();
      return;
    }
    welcomeRetryCount += 1;
    playWelcomeGreeting();
    if (welcomeRetryCount >= 10) {
      clearWelcomeRetryTimer();
    }
  }, 250);
}

function armWelcomeSpeechFallback() {
  const trySpeak = () => {
    playWelcomeGreeting();
    if (welcomeSpokenThisLoad) {
      window.removeEventListener('pointerdown', trySpeak, true);
      window.removeEventListener('keydown', trySpeak, true);
      window.removeEventListener('touchstart', trySpeak, true);
    }
  };
  window.addEventListener('pointerdown', trySpeak, true);
  window.addEventListener('keydown', trySpeak, true);
  window.addEventListener('touchstart', trySpeak, true);
}

function setAssetMasked() {
  const input = document.getElementById('assetInput');
  const btn = document.getElementById('assetToggleBtn');
  if (!input || !btn) return;
  assetVisible = false;
  input.value = '********';
  input.readOnly = true;
  input.classList.add('asset-masked');
  btn.textContent = '👁';
  btn.setAttribute('aria-label', '显示总资产');
}

function setAssetVisible() {
  const input = document.getElementById('assetInput');
  const btn = document.getElementById('assetToggleBtn');
  if (!input || !btn) return;
  assetVisible = true;
  input.value = assetRawValue;
  input.readOnly = false;
  input.classList.remove('asset-masked');
  btn.textContent = '🙈';
  btn.setAttribute('aria-label', '隐藏总资产');
  input.focus();
}

function bindAssetVisibilityControls() {
  const input = document.getElementById('assetInput');
  const btn = document.getElementById('assetToggleBtn');
  if (!input || !btn) return;

  btn.addEventListener('click', function () {
    if (assetVisible) {
      assetRawValue = input.value;
      setAssetMasked();
    } else {
      setAssetVisible();
    }
  });

  input.addEventListener('input', function () {
    if (assetVisible) {
      assetRawValue = input.value;
    }
  });
}

async function initCalendar() {
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  await selectDate(todayStr);

  try {
    await loadLastAsset();
  } catch (e) {
    console.warn('加载资产失败', e);
  }
}

async function loadCalendar(year, month) {
  const request = ++calendarLoadRequest;
  try {
    const response = await diaryFetch(`/get_calendar?year=${year}&month=${month}`);
    const data = await response.json();
    if (request !== calendarLoadRequest) return;
    document.getElementById('monthTitle').textContent = data.month_title || `${year}年${month}月`;
    renderCalendar(data.weeks || []);
  } catch (error) {
    console.error('日历加载失败:', error);
  }
}

function renderCalendar(weeks) {
  const tbody = document.getElementById('calendarBody');
  if (!tbody) return;

  tbody.innerHTML = weeks
    .map(
      (week) =>
        `<tr>${week
          .map((day) => {
            const dayDate = (day.date || '').split('T')[0];
            return `<td onclick="selectDate('${dayDate}')" class="${dayDate === selectedDate ? 'active-day' : ''} ${day.is_today ? 'today-highlight' : ''}"><div class="solar-day">${day.solar || ''}</div><div class="lunar-day">${day.lunar || ''}</div></td>`;
          })
          .join('')}</tr>`
    )
    .join('');
}

function formatDateTitle(dateStr) {
  const [y, m, d] = dateStr.split('-');
  return `${y}年${m}月${d}日日志`;
}

async function selectDate(dateStr) {
  const request = ++logLoadRequest;
  selectedDate = dateStr;
  loadedDate = null;
  currentDate = new Date(`${dateStr}T12:00:00`);
  // 先切换日历，避免较慢的日志回包把用户刚切换的月份改回去。
  const calendar = loadCalendar(currentDate.getFullYear(), currentDate.getMonth() + 1);
  try {
    const response = await diaryFetch(`/get_log?date=${dateStr}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const log = await response.json();
    if (request !== logLoadRequest) return;

    const logTitle = document.getElementById('logTitle');
    if (logTitle) {
      logTitle.textContent = formatDateTitle(dateStr);
    }

    LOG_FIELDS.forEach((field) => {
      const element = logField(field);
      if (!element) return;
      const sharedState = sharedFieldState.get(field);
      // 全局状态只在本次页面首次加载时填入；后续切日历保留当前草稿。
      if (sharedState?.initialized) return;
      element.value = (log && log[field]) || '';
      if (sharedState) sharedState.initialized = true;
    });
    loadedDate = dateStr;
    await calendar;
  } catch (error) {
    if (request === logLoadRequest) console.error('日志加载失败:', error);
  }
}

async function saveLog(e) {
  e.preventDefault();
  if (isSaving) return;

  if (!selectedDate) {
    alert('请先选择日期再保存。');
    return;
  }
  if (loadedDate !== selectedDate) {
    alert('日志仍在加载，请稍后保存。');
    return;
  }

  const saveBtn = document.getElementById('globalSaveBtn');
  isSaving = true;
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = '保存中...';
  }

  const formData = { date: selectedDate };
  // FormData 包含通过 form="logForm" 关联的左侧在途任务输入框。
  const fd = new FormData(e.target);
  for (const [k, v] of fd.entries()) {
    formData[k] = v;
  }

  const submittedRevisions = new Map(SHARED_FIELDS.map(field => [
    field, sharedFieldState.get(field).revision
  ]));

  try {
    const res = await diaryFetch('/save_log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formData)
    });

    if (!res.ok) {
      let errMsg = `HTTP ${res.status}`;
      try {
        const detail = await res.json();
        if (detail && detail.message) errMsg = detail.message;
      } catch (_) {}
      throw new Error(errMsg);
    }

    const result = await res.json();
    if (result.status !== 'success') {
      throw new Error(result.message || '服务器未确认保存成功');
    }
    SHARED_FIELDS.forEach(field => {
      const state = sharedFieldState.get(field);
      if (state.revision === submittedRevisions.get(field) &&
          logField(field)?.value === formData[field]) {
        state.initialized = true;
        state.dirty = false;
      }
    });

    const assetInput = document.getElementById('assetInput');
    const currentAssetValue = assetVisible && assetInput ? assetInput.value : assetRawValue;
    assetRawValue = currentAssetValue || '';

    try {
      await appendAssetIfChanged(assetRawValue.trim());
    } catch (err) {
      console.warn('追加资产时出错', err);
    }

    alert('日志保存成功。');
    playUiTone('success');
  } catch (error) {
    if (window.diarySession?.expired) return;
    console.error('保存失败:', error);
    alert(`保存失败，请重试：\n${error.message || ''}`);
    playUiTone('error');
  } finally {
    isSaving = false;
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = '💾 保存日志';
    }
  }
}

async function exportLogsToExcel() {
  const ok = window.confirm('是否确认导出全部日志到Excel？');
  if (!ok) return;

  const btn = document.getElementById('exportExcelBtn');
  const oldText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '导出中...';
  }

  try {
    const resp = await diaryFetch('/export_logs_excel', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok || data.status !== 'success') {
      throw new Error((data && data.message) || `HTTP ${resp.status}`);
    }
    alert(`导出成功。\n共 ${data.row_count} 条日志。\n文件位置：${data.output_path}`);
    playUiTone('success');
  } catch (err) {
    if (window.diarySession?.expired) return;
    alert(`导出失败：${err.message || err}`);
    playUiTone('error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || '导出日志到Excel';
    }
  }
}

async function submitPasswordChange() {
  const oldPwdInput = document.getElementById('oldPasswordInput');
  const newPwdInput = document.getElementById('newPasswordInput');
  const confirmPwdInput = document.getElementById('confirmNewPasswordInput');
  const btn = document.getElementById('confirmChangePasswordBtn');
  if (!oldPwdInput || !newPwdInput || !confirmPwdInput || !btn) return;

  const oldPassword = (oldPwdInput.value || '').trim();
  const newPassword = (newPwdInput.value || '').trim();
  const confirmPassword = (confirmPwdInput.value || '').trim();
  if (!oldPassword || !newPassword || !confirmPassword) {
    alert('请完整填写旧密码、新密码、确认新密码。');
    return;
  }

  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '提交中...';
  try {
    const resp = await diaryFetch('/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        old_password: oldPassword,
        new_password: newPassword,
        confirm_password: confirmPassword
      })
    });
    const data = await resp.json();
    if (!resp.ok || data.status !== 'success') {
      throw new Error((data && data.message) || `HTTP ${resp.status}`);
    }
    alert('修改成功，请重新登录。');
    window.location.href = data.redirect || diaryUrl('/login');
  } catch (err) {
    if (window.diarySession?.expired) return;
    alert(`修改失败：${err.message || err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText || '确认修改';
  }
}

async function loadLastAsset() {
  const resp = await diaryFetch('/get_last_asset');
  if (!resp.ok) throw new Error('加载失败');
  const data = await resp.json();
  assetRawValue = data.last_line || '';
  setAssetMasked();
}

async function appendAssetIfChanged(value) {
  if (!value) return { appended: false };
  const resp = await diaryFetch('/append_asset_if_changed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value })
  });
  if (!resp.ok) throw new Error('append failed');
  return await resp.json();
}

function changeMonth(offset) {
  currentDate.setDate(1);
  currentDate.setMonth(currentDate.getMonth() + offset);
  loadCalendar(currentDate.getFullYear(), currentDate.getMonth() + 1);
}

document.addEventListener('DOMContentLoaded', async function () {
  bindAssetVisibilityControls();
  bindSharedFieldDrafts();
  initCalendar();

  document.addEventListener('keydown', function (e) {
    const key = e.key || '';
    if ((e.ctrlKey || e.metaKey) && (key === 's' || key === 'S')) {
      e.preventDefault();
      const form = document.getElementById('logForm');
      if (form) {
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit();
        } else {
          form.submit();
        }
      }
    }
  });

  const exportExcelBtn = document.getElementById('exportExcelBtn');
  if (exportExcelBtn) {
    exportExcelBtn.addEventListener('click', exportLogsToExcel);
  }

  const mainMenuBtn = document.getElementById('mainMenuBtn');
  const mainMenuPanel = document.getElementById('mainMenuPanel');
  const helpMenuBtn = document.getElementById('helpMenuBtn');
  const helpModal = document.getElementById('helpModal');
  const helpModalCloseBtn = document.getElementById('helpModalCloseBtn');
  const helpModalBackdrop = document.getElementById('helpModalBackdrop');
  const changePasswordBtn = document.getElementById('changePasswordBtn');
  const passwordModal = document.getElementById('passwordModal');
  const passwordModalCloseBtn = document.getElementById('passwordModalCloseBtn');
  const passwordModalBackdrop = document.getElementById('passwordModalBackdrop');
  const confirmChangePasswordBtn = document.getElementById('confirmChangePasswordBtn');
  const oldPasswordInput = document.getElementById('oldPasswordInput');
  const newPasswordInput = document.getElementById('newPasswordInput');
  const confirmNewPasswordInput = document.getElementById('confirmNewPasswordInput');
  const uiSoundToggle = document.getElementById('uiSoundToggle');
  const uiFxToggle = document.getElementById('uiFxToggle');
  await loadUiSettings();
  applyUiFxState();
  if (uiFxToggle) {
    uiFxToggle.checked = uiFxEnabled;
    uiFxToggle.addEventListener('change', () => {
      uiFxEnabled = uiFxToggle.checked;
      applyUiFxState();
      saveUiSettings();
    });
  }
  if (uiSoundToggle) {
    uiSoundToggle.checked = uiSoundEnabled;
    uiSoundToggle.addEventListener('change', () => {
      uiSoundEnabled = uiSoundToggle.checked;
      playUiTone('tap');
      saveUiSettings();
    });
  }
  if (mainMenuBtn && mainMenuPanel) {
    mainMenuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      mainMenuPanel.classList.toggle('d-none');
      playUiTone('tap');
    });
    document.addEventListener('click', (e) => {
      if (!mainMenuPanel.contains(e.target) && e.target !== mainMenuBtn) {
        mainMenuPanel.classList.add('d-none');
      }
    });
  }

  const openHelpModal = () => {
    if (!helpModal) return;
    helpModal.classList.remove('d-none');
    if (mainMenuPanel) mainMenuPanel.classList.add('d-none');
    playUiTone('tap');
  };
  const closeHelpModal = () => {
    if (!helpModal) return;
    helpModal.classList.add('d-none');
    playUiTone('tap');
  };
  if (helpMenuBtn) helpMenuBtn.addEventListener('click', openHelpModal);
  if (helpModalCloseBtn) helpModalCloseBtn.addEventListener('click', closeHelpModal);
  if (helpModalBackdrop) helpModalBackdrop.addEventListener('click', closeHelpModal);
  const openPasswordModal = () => {
    if (!passwordModal) return;
    if (oldPasswordInput) oldPasswordInput.value = '';
    if (newPasswordInput) newPasswordInput.value = '';
    if (confirmNewPasswordInput) confirmNewPasswordInput.value = '';
    passwordModal.classList.remove('d-none');
    if (mainMenuPanel) mainMenuPanel.classList.add('d-none');
    playUiTone('tap');
  };
  const closePasswordModal = () => {
    if (!passwordModal) return;
    passwordModal.classList.add('d-none');
    playUiTone('tap');
  };
  if (changePasswordBtn) changePasswordBtn.addEventListener('click', openPasswordModal);
  if (passwordModalCloseBtn) passwordModalCloseBtn.addEventListener('click', closePasswordModal);
  if (passwordModalBackdrop) passwordModalBackdrop.addEventListener('click', closePasswordModal);
  if (confirmChangePasswordBtn) confirmChangePasswordBtn.addEventListener('click', submitPasswordChange);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && helpModal && !helpModal.classList.contains('d-none')) {
      closeHelpModal();
    }
    if (e.key === 'Escape' && passwordModal && !passwordModal.classList.contains('d-none')) {
      closePasswordModal();
    }
  });

  // 每次刷新/进入页面都尝试欢迎音频；若被拦截，则在首次用户交互时补播
  setTimeout(scheduleWelcomeAttempts, 80);
  armWelcomeSpeechFallback();
  window.addEventListener('pageshow', () => {
    welcomeSpokenThisLoad = false;
    setTimeout(scheduleWelcomeAttempts, 80);
    armWelcomeSpeechFallback();
  });
});
