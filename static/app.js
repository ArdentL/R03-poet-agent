'use strict';

const byId = (id) => document.getElementById(id);
const settingsDialog = byId('settings-dialog');
const settingsForm = byId('settings-form');
const state = { config: null };
const providerLabels = { deepseek: 'DeepSeek', doubao: '豆包' };
let toastTimer;

async function api(path, data, { audio = false } = {}) {
  const options = { cache: 'no-store' };
  if (data !== undefined) {
    const token = document.cookie.match(/(?:^|;\s*)poetry_token=([^;]+)/)?.[1] || '';
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-Poetry-Token': token };
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch('/api/' + path, options);
  } catch {
    throw new Error('本地应用未响应。请确认启动窗口仍在运行。');
  }
  if (audio && response.ok && response.headers.get('Content-Type') === 'audio/mpeg') return response.blob();
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || '请求失败（' + response.status + '）。');
  if (audio) throw new Error('服务未返回 MP3 音频。');
  return result;
}

function feedback(element, message = '', error = false) {
  element.textContent = message;
  element.classList.toggle('error', error);
}

function notify(message) {
  const toast = byId('toast');
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4400);
}


function updateConnection() {
  const ready = Boolean(state.config?.api_ready);
  const label = providerLabels[state.config?.provider] || '模型';
  byId('connection-dot').classList.toggle('ready', ready);
  byId('connection-text').textContent = ready ? `${label} 已配置` : '模型设置';
  byId('settings-button').disabled = !state.config;
  window.dispatchEvent(new Event('poetry-config-updated'));
}

function fillProvider() {
  const provider = settingsForm.elements.provider.value;
  const config = state.config.providers[provider];
  settingsForm.elements.base_url.value = config.base_url;
  settingsForm.elements.model.value = config.model;
  settingsForm.elements.api_key.value = '';
  settingsForm.elements.api_key.placeholder = config.api_ready ? '已配置，留空保留' : `填写${providerLabels[provider]} API Key`;
}

function openSettings(message = '') {
  if (!state.config) return;
  settingsForm.elements.provider.value = state.config.provider;
  fillProvider();
  feedback(byId('settings-feedback'), message);
  settingsDialog.showModal();
}

byId('settings-button').addEventListener('click', () => openSettings());
settingsForm.elements.provider.addEventListener('change', fillProvider);
settingsForm.querySelector('.close-button').addEventListener('click', () => settingsDialog.close());
settingsDialog.addEventListener('close', () => { settingsForm.elements.api_key.value = ''; });
settingsDialog.addEventListener('cancel', (event) => {
  if (byId('save-settings').disabled) event.preventDefault();
});
settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const controls = [...settingsForm.elements];
  const data = Object.fromEntries(new FormData(settingsForm));
  controls.forEach((control) => { control.disabled = true; });
  feedback(byId('settings-feedback'), '正在保存…');
  try {
    const { config } = await api('settings', data);
    state.config = config;
    updateConnection();
    settingsDialog.close();
    notify('模型设置已保存。');
  } catch (error) {
    feedback(byId('settings-feedback'), error.message, true);
  } finally {
    controls.forEach((control) => { control.disabled = false; });
  }
});

async function boot() {
  updateConnection();
  try {
    const { config } = await api('start');
    state.config = config;
    updateConnection();
  } catch (error) {
    feedback(byId('poetry-feedback'), error.message + ' 请刷新页面重试。', true);
  }
}

boot();
