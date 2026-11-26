'use strict';

const byId = (id) => document.getElementById(id);
const briefForm = byId('story-brief');
const settingsDialog = byId('settings-dialog');
const settingsForm = byId('settings-form');
const sendDialog = byId('send-dialog');
const sendForm = byId('send-form');
const storyTitle = byId('story-title');
const storyBody = byId('story-body');
const premise = byId('premise');
const maxChars = byId('max-chars');
const revision = byId('revision');
const generateIdeas = byId('generate-ideas');
const generateStory = byId('generate-story');
const reviseStory = byId('revise-story');
const briefFields = [premise, byId('genre'), byId('requirements'), maxChars];
const draftFields = [storyTitle, storyBody, revision];
const settingFields = ['base_url', 'model', 'smtp_host', 'smtp_port', 'smtp_security', 'smtp_user', 'smtp_from', 'recipient'];
const storageKey = 'story-workbench-v1';
const state = {
  config: null, ideas: [], outline: [], selected: -1, source: '', generating: null, checkVersion: 0, mail: null,
};
let checkTimer;
let toastTimer;

async function api(path, data, { audio = false } = {}) {
  const options = { cache: 'no-store' };
  if (data !== undefined) {
    const token = document.cookie.match(/(?:^|;\s*)story_token=([^;]+)/)?.[1] || '';
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-Story-Token': token };
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

function readDraft() {
  return {
    premise: premise.value.trim(),
    genre: byId('genre').value.trim(),
    requirements: byId('requirements').value.trim(),
    max_chars: maxChars.value ? maxChars.valueAsNumber : null,
    title: storyTitle.value.trim(),
    body: storyBody.value,
  };
}

function saveDraft() {
  try {
    const fields = Object.fromEntries([...briefFields, ...draftFields].map((field) => [field.id, field.value]));
    const { ideas, outline, selected, source } = state;
    localStorage.setItem(storageKey, JSON.stringify({ fields, ideas, outline, selected, source }));
  } catch {
    // The editor can still be used when browser storage is unavailable.
  }
}

function restoreDraft() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey)) || {};
    for (const field of [...briefFields, ...draftFields]) {
      const value = saved.fields?.[field.id];
      if (typeof value === 'string') field.value = value;
    }
    if (Array.isArray(saved.ideas)) state.ideas = saved.ideas;
    if (Array.isArray(saved.outline)) state.outline = saved.outline;
    if (Number.isInteger(saved.selected) && state.ideas[saved.selected]) state.selected = saved.selected;
    if (typeof saved.source === 'string') state.source = saved.source;
  } catch {
    // Start with an empty draft if browser data cannot be read.
  }
}

function updateConnection() {
  const ready = Boolean(state.config?.api_ready);
  byId('connection-dot').classList.toggle('ready', ready);
  byId('connection-text').textContent = ready ? 'DeepSeek 已配置' : '配置 DeepSeek';
  byId('settings-button').disabled = !state.config;
  window.dispatchEvent(new Event('story-config-updated'));
}

function updateControls() {
  const hasBody = Boolean(storyBody.value.trim());
  const loading = Boolean(state.generating);
  const canGenerate = Boolean(state.config) && !loading;
  generateIdeas.disabled = !canGenerate;
  generateStory.disabled = !canGenerate;
  reviseStory.disabled = !canGenerate || !hasBody || !revision.value.trim();
  byId('copy-story').disabled = !hasBody;
  byId('download-story').disabled = !hasBody;
  byId('send-story').disabled = !state.config || !hasBody || !storyTitle.value.trim();
  byId('check-story').disabled = !state.config || !hasBody;
  byId('review-panel').hidden = !hasBody;
  byId('revision-panel').hidden = !hasBody;
  briefFields.forEach((field) => { field.readOnly = loading; });
  draftFields.forEach((field) => { field.readOnly = loading && state.generating !== 'ideas'; });
  for (const button of byId('ideas').children) button.disabled = loading;
  const label = { generated: '生成稿', edited: '已编辑', revised: '已改稿' }[state.source];
  const badge = byId('draft-state');
  badge.textContent = label || '';
  badge.hidden = !label || !(hasBody || storyTitle.value.trim());
}

function updateIdeaSelection() {
  for (const [index, button] of Array.from(byId('ideas').children).entries()) {
    button.setAttribute('aria-pressed', String(state.selected === index));
  }
  const hint = byId('idea-hint');
  hint.hidden = !state.ideas.length;
  hint.textContent = state.selected >= 0 ? '已选构思' : '点选一个方向';
}

function renderIdeas() {
  byId('ideas-empty').hidden = Boolean(state.ideas.length);
  const nodes = state.ideas.map((suggestion, index) => {
    const button = document.createElement('button');
    button.className = 'idea';
    button.type = 'button';
    const title = document.createElement('strong');
    const text = document.createElement('span');
    title.textContent = suggestion.title;
    text.textContent = suggestion.text;
    button.append(title, text);
    button.addEventListener('click', () => {
      state.selected = state.selected === index ? -1 : index;
      updateIdeaSelection();
      saveDraft();
    });
    return button;
  });
  byId('ideas').replaceChildren(...nodes);
  updateIdeaSelection();
}

function renderOutline() {
  const panel = byId('outline-panel');
  panel.hidden = !state.outline.length;
  byId('outline').replaceChildren(...state.outline.map((entry) => {
    const step = document.createElement('li');
    const label = document.createElement('strong');
    label.textContent = entry.title;
    step.append(label, document.createTextNode(entry.text));
    return step;
  }));
}

function renderChecks(checks) {
  const labels = { pass: '通过', warn: '需修改', check: '请审阅' };
  byId('checks').replaceChildren(...checks.map((check) => {
    const row = document.createElement('li');
    const status = document.createElement('span');
    const description = document.createElement('div');
    const title = document.createElement('strong');
    const detail = document.createElement('span');
    const key = Object.hasOwn(labels, check.status) ? check.status : 'check';
    status.className = 'status ' + key;
    status.textContent = labels[key];
    title.textContent = check.label;
    detail.className = 'check-detail';
    detail.textContent = check.detail;
    description.append(title, detail);
    row.append(status, description);
    return row;
  }));
}

async function checkDraft(version, manual = false) {
  if (!maxChars.validity.valid) {
    renderChecks([]);
    if (manual) maxChars.reportValidity();
    return;
  }
  if (!storyBody.value.trim() || !state.config) return;
  try {
    const { checks } = await api('check', readDraft());
    if (state.checkVersion !== version) return;
    renderChecks(checks);
    if (manual) notify('要求检查已更新。');
  } catch (error) {
    if (manual && state.checkVersion === version) notify(error.message);
  }
}

function scheduleCheck() {
  clearTimeout(checkTimer);
  const version = ++state.checkVersion;
  checkTimer = setTimeout(() => checkDraft(version), 350);
}

function openSettings(message = '') {
  if (!state.config) return;
  for (const name of settingFields) settingsForm.elements[name].value = state.config[name] ?? '';
  settingsForm.elements.api_key.value = '';
  settingsForm.elements.smtp_password.value = '';
  settingsForm.elements.api_key.placeholder = state.config.api_ready ? '已配置，留空保留' : '粘贴 DeepSeek API Key';
  settingsForm.elements.smtp_password.placeholder = state.config.smtp_ready ? '已配置，留空保留' : '填写邮箱服务提供的授权码';
  feedback(byId('settings-feedback'), message);
  settingsDialog.showModal();
}

async function generate(mode) {
  if (!state.config || state.generating) return;
  const isRevision = mode === 'revise';
  const isIdeas = mode === 'ideas';
  const status = byId(isRevision ? 'revision-feedback' : 'generation-feedback');
  const draft = readDraft();
  if (!isRevision && !draft.premise) {
    feedback(status, '请填写题目或故事线索。', true);
    premise.focus();
    return;
  }
  if (!maxChars.reportValidity()) return;
  if (isRevision && (!draft.body.trim() || !revision.value.trim())) return;
  if (!state.config.api_ready) {
    openSettings('保存 DeepSeek API Key 后即可生成。');
    return;
  }

  const idea = state.ideas[state.selected];
  const selectedIdea = idea && mode === 'draft' ? idea.title + '\n' + idea.text : '';
  const { body, title, ...spec } = draft;
  const request = { ...spec, mode, selected_idea: selectedIdea };
  if (isRevision) Object.assign(request, { title, body, revision: revision.value.trim() });
  state.generating = mode;
  feedback(status, { ideas: '正在生成构思…', draft: '正在写故事…', revise: '正在按反馈改稿…' }[mode]);
  if (!isIdeas) {
    clearTimeout(checkTimer);
    state.checkVersion += 1;
  }
  updateControls();
  try {
    const { result } = await api('generate', request);
    if (isIdeas) {
      state.ideas = result.ideas;
      state.selected = -1;
      renderIdeas();
      feedback(status, '构思已生成。');
    } else {
      state.outline = result.outline;
      storyTitle.value = result.title;
      storyBody.value = result.body;
      state.source = isRevision ? 'revised' : 'generated';
      byId('outline-panel').open = true;
      renderOutline();
      renderChecks(result.checks);
      feedback(status, isRevision ? '改稿已完成，可继续编辑。' : '故事已生成，可直接编辑。');
      if (window.matchMedia('(max-width: 680px)').matches) {
        byId('draft-title').scrollIntoView({ block: 'start' });
      }
    }
    saveDraft();
  } catch (error) {
    feedback(status, error.message, true);
    if (!isIdeas) scheduleCheck();
  } finally {
    state.generating = null;
    updateControls();
  }
}

briefForm.addEventListener('submit', (event) => {
  event.preventDefault();
  generate(event.submitter === generateStory ? 'draft' : 'ideas');
});
reviseStory.addEventListener('click', () => generate('revise'));
byId('check-story').addEventListener('click', () => {
  clearTimeout(checkTimer);
  checkDraft(++state.checkVersion, true);
});

for (const field of [...briefFields, ...draftFields]) {
  field.addEventListener('input', () => {
    if (field === storyTitle || field === storyBody) state.source = 'edited';
    if (field === premise && state.selected !== -1) {
      state.selected = -1;
      updateIdeaSelection();
    }
    saveDraft();
    updateControls();
    if (field !== revision) scheduleCheck();
  });
}

function storyText() {
  const { title, body } = readDraft();
  return [title, body.replace(/^\n+|\n+$/g, '')].filter(Boolean).join('\n\n');
}

byId('copy-story').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(storyText());
    notify('已复制故事。');
  } catch {
    notify('浏览器无法访问剪贴板。可以下载 TXT。');
  }
});

byId('download-story').addEventListener('click', () => {
  const name = storyTitle.value.trim().replace(/[<>:"/\\|?*]/g, '_').slice(0, 60) || '故事';
  const url = URL.createObjectURL(new Blob(['\uFEFF', storyText(), '\n'], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name + '.txt';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

byId('settings-button').addEventListener('click', () => openSettings());
settingsForm.elements.smtp_security.addEventListener('change', () => {
  const { smtp_security: security, smtp_port: port } = settingsForm.elements;
  if (security.value === 'starttls' && port.value === '465') port.value = '587';
  if (security.value === 'ssl' && port.value === '587') port.value = '465';
});
settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = byId('save-settings');
  if (button.disabled) return;
  const close = settingsForm.querySelector('.close-button');
  button.disabled = true;
  close.disabled = true;
  feedback(byId('settings-feedback'), '正在保存…');
  try {
    const data = Object.fromEntries(new FormData(settingsForm));
    const { config } = await api('settings', data);
    state.config = config;
    updateConnection();
    settingsDialog.close();
    notify('设置已保存。');
  } catch (error) {
    feedback(byId('settings-feedback'), error.message, true);
  } finally {
    button.disabled = false;
    close.disabled = false;
  }
});
settingsDialog.addEventListener('cancel', (event) => {
  if (byId('save-settings').disabled) event.preventDefault();
});
settingsDialog.addEventListener('close', () => {
  settingsForm.elements.api_key.value = '';
  settingsForm.elements.smtp_password.value = '';
});

byId('send-story').addEventListener('click', () => {
  if (!state.config.smtp_ready) {
    openSettings('填写并保存 SMTP 设置后即可发送。');
    return;
  }
  const { title, body } = readDraft();
  state.mail = { title, body: body.replace(/^\n+|\n+$/g, '') };
  byId('mail-title').textContent = state.mail.title;
  byId('mail-body').textContent = state.mail.body;
  sendForm.elements.recipient.value = state.config.recipient || '';
  feedback(byId('send-feedback'));
  sendDialog.showModal();
  sendForm.elements.recipient.focus();
});

for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => byId(button.dataset.close).close());
}

sendForm.elements.recipient.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.isComposing) return;
  event.preventDefault();
  if (sendForm.elements.recipient.reportValidity()) byId('confirm-send').focus();
});

sendDialog.addEventListener('cancel', (event) => {
  if (byId('confirm-send').disabled) event.preventDefault();
});

sendForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = byId('confirm-send');
  if (!state.mail || button.disabled) return;
  const controls = [...sendForm.querySelectorAll('input, button')];
  const recipient = sendForm.elements.recipient.value.trim();
  for (const control of controls) control.disabled = true;
  feedback(byId('send-feedback'), '正在发送邮件…');
  try {
    const response = await api('send', { ...state.mail, recipient });
    sendDialog.close();
    notify('邮件已发送至 ' + response.recipient);
  } catch (error) {
    feedback(byId('send-feedback'), error.message, true);
  } finally {
    for (const control of controls) control.disabled = false;
  }
});

async function boot() {
  restoreDraft();
  renderIdeas();
  renderOutline();
  updateConnection();
  updateControls();
  try {
    const { config } = await api('start');
    state.config = config;
    updateConnection();
    updateControls();
    if (storyBody.value.trim()) scheduleCheck();
  } catch (error) {
    feedback(byId('generation-feedback'), error.message + ' 请刷新页面重试。', true);
  }
}

boot();
