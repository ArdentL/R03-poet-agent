'use strict';

(() => {
  const form = byId('poetry-brief');
  const topic = byId('poetry-topic');
  const source = byId('poetry-source');
  const poetryForm = byId('poetry-form');
  const cipai = byId('poetry-cipai');
  const seconds = byId('poetry-seconds');
  const title = byId('poem-title');
  const author = byId('poem-author');
  const era = byId('poem-era');
  const body = byId('poem-body');
  const prompt = byId('tts-prompt');
  const audioButton = byId('generate-audio');
  const audioPlayer = byId('poetry-audio');
  const audioDownload = byId('download-audio');
  const audioStatus = byId('audio-feedback');
  const sourceNote = byId('poem-source-note');
  const status = byId('poetry-feedback');
  const fields = [topic, source, poetryForm, cipai, seconds, byId('poetry-voice'), byId('poetry-soundscape'),
    byId('poetry-requirements'), title, author, era, body, prompt];
  const storage = 'poetry-workbench-v1';
  const kindRadios = [...form.querySelectorAll('[name="poetry-kind"]')];
  let loading = false;
  let audioLoading = false;
  let generatedPrompt = '';
  let audioUrl = '';
  let fingerprint = '';
  let checkVersion = 0;
  let checkTimer;
  let performanceJob = null;
  let performanceBusy = false;
  let performanceLoaded = false;
  let renderedPerformance = '';
  let lastPerformanceView = '';
  let performanceTimer;
  const performanceStatus = byId('performance-feedback');
  const terminalStates = ['succeeded', 'failed', 'cancelled', 'unknown'];

  function read() {
    return {
      kind: kindRadios.find((radio) => radio.checked).value,
      form: poetryForm.value, cipai: poetryForm.value === 'ci' ? cipai.value.trim() : '',
      topic: topic.value.trim(), source_text: source.value.trim(),
      voice: byId('poetry-voice').value.trim(), soundscape: byId('poetry-soundscape').value.trim(),
      requirements: byId('poetry-requirements').value.trim(),
      target_seconds: seconds.value ? seconds.valueAsNumber : null,
      title: title.value.trim(), author: author.value.trim(), era: era.value.trim(),
      poem_body: body.value, tts_prompt: prompt.value,
    };
  }

  function currentFingerprint() {
    const { tts_prompt, ...draft } = read();
    return JSON.stringify(draft);
  }

  function save() {
    try {
      localStorage.setItem(storage, JSON.stringify({
        fields: Object.fromEntries(fields.map((field) => [field.id, field.value])),
        kind: read().kind,
        note: sourceNote.textContent, fingerprint,
      }));
    } catch {
      // Editing remains available if browser storage is full or disabled.
    }
  }

  function update() {
    const busy = loading || performanceBusy;
    const hasBody = Boolean(body.value.trim());
    const hasPrompt = Boolean(prompt.value.trim());
    byId('generate-poetry').disabled = busy;
    byId('perform-poetry').disabled = busy || audioLoading || !performanceLoaded;
    byId('cancel-performance').disabled = !performanceBusy;
    byId('refresh-tts').disabled = busy || !hasBody;
    byId('copy-poem').disabled = !hasBody;
    byId('download-poem').disabled = !hasBody;
    byId('copy-tts').disabled = !hasPrompt;
    byId('download-tts').disabled = !hasPrompt;
    const text = prompt.value.trim();
    const promptLength = Array.from(text).length;
    const overLimit = promptLength > 3000;
    audioButton.disabled = audioLoading || busy || !hasPrompt || overLimit;
    if (audioUrl && text !== generatedPrompt) clearAudio();
    if (!audioLoading && overLimit) {
      feedback(audioStatus, `音频生成最多支持 3000 个字符，当前 ${promptLength} 个。`, true);
    } else if (!audioLoading && audioStatus.dataset.overLimit === 'true') {
      feedback(audioStatus);
    }
    if (!audioLoading) audioStatus.dataset.overLimit = String(overLimit);
    byId('check-poetry').disabled = busy;
    byId('poetry-review').hidden = !hasBody && !hasPrompt;
    byId('poetry-stale').hidden = !hasPrompt || !fingerprint || fingerprint === currentFingerprint();
    fields.forEach((field) => { field.readOnly = busy; });
    poetryForm.disabled = busy;
    byId('poetry-cipai-field').hidden = poetryForm.value !== 'ci';
    kindRadios.forEach((radio) => { radio.disabled = busy; });
    const original = read().kind === 'original';
    byId('poetry-source-field').hidden = original;
    byId('poetry-topic-label').textContent = original ? '创作主题' : '诗词题目';
    topic.placeholder = original ? '例如：春日校园，珍惜少年时光' : '例如：长歌行';
  }

  function render(checks) {
    const labels = { pass: '通过', warn: '需修改', check: '请审阅' };
    byId('poetry-checks').replaceChildren(...checks.map((check) => {
      const row = document.createElement('li');
      const badge = document.createElement('span');
      const description = document.createElement('div');
      const label = document.createElement('strong');
      const detail = document.createElement('span');
      const key = Object.hasOwn(labels, check.status) ? check.status : 'check';
      badge.className = 'status ' + key;
      badge.textContent = labels[key];
      label.textContent = check.label;
      detail.className = 'check-detail';
      detail.textContent = check.detail;
      description.append(label, detail);
      row.append(badge, description);
      return row;
    }));
  }

  async function check(version = ++checkVersion) {
    if (!seconds.validity.valid || loading || performanceBusy || !(body.value.trim() || prompt.value.trim())) return;
    try {
      const response = await api('poetry/check', read());
      if (version !== checkVersion) return;
      render(response.checks);
      feedback(byId('poetry-check-feedback'));
    } catch (error) {
      if (version === checkVersion) feedback(byId('poetry-check-feedback'), error.message, true);
    }
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    const version = ++checkVersion;
    checkTimer = setTimeout(() => check(version), 400);
  }

  async function generate(useCurrentBody) {
    if (loading || performanceBusy || !form.reportValidity()) return;
    if (!state.config) {
      feedback(status, '连接设置正在加载，请稍后重试。', true);
      return;
    }
    if (!state.config.api_ready) {
      openSettings('保存当前模型服务的 API Key 后即可生成诗词和朗诵提示词。');
      return;
    }
    const request = read();
    if (useCurrentBody && !request.poem_body.trim()) return;
    if (!useCurrentBody) {
      request.poem_body = '';
      request.title = '';
      delete request.author;
      delete request.era;
    }
    if (!(request.topic || request.poem_body.trim() || (request.kind === 'existing' && request.source_text))) {
      feedback(status, '请填写诗词题目、创作主题或粘贴原文。', true);
      topic.focus();
      return;
    }
    delete request.tts_prompt;
    loading = true;
    clearTimeout(checkTimer);
    checkVersion += 1;
    update();
    feedback(status, useCurrentBody ? '正在按当前正文更新朗诵安排…' : '正在生成诗词与朗诵提示词…');
    try {
      const { result } = await api('poetry/generate', request);
      title.value = result.title;
      author.value = result.author;
      era.value = result.era;
      body.value = result.body;
      prompt.value = result.tts_prompt;
      sourceNote.textContent = result.source_note;
      fingerprint = currentFingerprint();
      render(result.checks);
      feedback(byId('poetry-check-feedback'));
      feedback(status, '已生成，朗诵预估结束于 ' + result.duration_seconds.toFixed(1) + ' 秒。');
      save();
      if (window.matchMedia('(max-width: 680px)').matches) {
        byId('poetry-draft-title').scrollIntoView({ block: 'start' });
      }
    } catch (error) {
      feedback(status, error.message, true);
    } finally {
      loading = false;
      update();
    }
  }

  function poemText() {
    return [title.value.trim(), [era.value.trim(), author.value.trim()].filter(Boolean).join(' · '),
      body.value.trim()].filter(Boolean).join('\n\n');
  }

  function clearAudio() {
    audioPlayer.pause();
    audioPlayer.removeAttribute('src');
    audioPlayer.load();
    audioDownload.removeAttribute('href');
    audioPlayer.hidden = audioDownload.hidden = true;
    URL.revokeObjectURL(audioUrl);
    audioUrl = '';
    feedback(audioStatus);
  }

  async function generateAudio() {
    const text = prompt.value.trim();
    if (audioLoading || loading || performanceBusy || !text || Array.from(text).length > 3000) return;
    const name = title.value.trim().replace(/[<>:"/\\|?*]/g, '_').slice(0, 60) || '诗词';
    audioLoading = true;
    update();
    feedback(audioStatus, '正在合成朗诵音频…');
    try {
      const result = await api('poetry/audio', { tts_prompt: text }, { audio: true });
      if (prompt.value.trim() !== text) {
        feedback(audioStatus, '提示词已修改，请用当前版本重新生成音频。');
        return;
      }
      if (audioUrl) clearAudio();
      generatedPrompt = text;
      audioUrl = URL.createObjectURL(result);
      audioPlayer.src = audioUrl;
      audioDownload.href = audioUrl;
      audioDownload.download = `${name}-朗诵.mp3`;
      audioPlayer.hidden = audioDownload.hidden = false;
      feedback(audioStatus, '朗诵音频已生成。');
    } catch (error) {
      feedback(audioStatus, error.message, true);
    } finally {
      audioLoading = false;
      update();
    }
  }

  function showPerformance(job) {
    performanceLoaded = true;
    performanceJob = job;
    performanceBusy = Boolean(job && !terminalStates.includes(job.state));
    if (!job) { update(); return; }
    byId('performance-panel').hidden = false;
    feedback(performanceStatus, job.message, ['failed', 'unknown'].includes(job.state));
    if (job.poem && renderedPerformance !== job.id && (performanceBusy || !body.value.trim())) {
      const poem = job.poem;
      title.value = poem.title;
      author.value = poem.author;
      era.value = poem.era;
      body.value = poem.body;
      prompt.value = poem.tts_prompt;
      sourceNote.textContent = poem.source_note;
      fingerprint = currentFingerprint();
      render(poem.checks);
      renderedPerformance = job.id;
      save();
    }
    const actionStates = { requested: '已请求', started: '执行中', ended: '已完成', interrupted: '已中断', failed: '失败' };
    byId('performance-actions').textContent = Object.values(job.actions || {})
      .map((action) => `${action.name}：${actionStates[action.state] || action.state}`).join('；');
    const view = JSON.stringify([job.id, job.sequence]);
    if (lastPerformanceView !== view) {
      lastPerformanceView = view;
      byId('performance-sequence').replaceChildren(...(job.sequence || []).map((entry, index) => {
      const row = document.createElement('li');
      const verse = document.createElement('strong');
      const cue = document.createElement('span');
      verse.textContent = job.poem.segments[index].text;
      cue.textContent = entry.expression + (entry.action ? ' · ' + entry.action : '');
      row.append(verse, cue);
      return row;
    }));
    }
    const url = `/api/performances/${job.id}/audio`;
    if (job.audio_ready && renderedPerformance === job.id && audioUrl !== url) {
      clearAudio();
      generatedPrompt = job.poem.tts_prompt;
      audioUrl = url;
      audioPlayer.src = audioDownload.href = url;
      audioDownload.download = '机器人朗诵.mp3';
      audioPlayer.hidden = audioDownload.hidden = false;
    }
    update();
  }

  async function watchPerformance() {
    clearTimeout(performanceTimer);
    try {
      const path = performanceJob ? `performances/${performanceJob.id}` : 'performances';
      const { performance } = await api(path);
      showPerformance(performance);
      if (performanceBusy) performanceTimer = setTimeout(watchPerformance, 1000);
    } catch (error) {
      byId('performance-panel').hidden = false;
      feedback(performanceStatus, error.message + ' 正在重新查询任务状态…', true);
      performanceTimer = setTimeout(watchPerformance, 3000);
    }
  }

  async function performPoetry() {
    if (loading || audioLoading || performanceBusy || !form.reportValidity()) return;
    if (!state.config?.api_ready) {
      openSettings('保存当前模型服务的 API Key 后即可让机器人吟诗。');
      return;
    }
    const request = { ...read(), id: 'job-' + crypto.randomUUID().replaceAll('-', '') };
    performanceBusy = true;
    update();
    byId('performance-panel').hidden = false;
    feedback(performanceStatus, '正在提交吟诗任务…');
    try {
      const { performance } = await api('performances', request);
      showPerformance(performance);
      await watchPerformance();
    } catch (error) {
      performanceJob = null;
      performanceLoaded = false;
      await watchPerformance();
      feedback(performanceStatus, error.message, true);
    }
  }

  async function cancelPerformance() {
    if (!performanceJob || !performanceBusy) return;
    byId('cancel-performance').disabled = true;
    try {
      const { performance } = await api(`performances/${performanceJob.id}/cancel`, {});
      showPerformance(performance);
    } catch (error) {
      feedback(performanceStatus, error.message, true);
      update();
    }
  }

  async function copy(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      notify('已复制' + label + '。');
    } catch {
      notify('浏览器无法访问剪贴板，可以下载 TXT。');
    }
  }

  function download(text, suffix) {
    const name = title.value.trim().replace(/[<>:"/\\|?*]/g, '_').slice(0, 60) || '诗词';
    const url = URL.createObjectURL(new Blob(['\uFEFF', text, '\n'], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = name + suffix + '.txt';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  form.addEventListener('submit', (event) => { event.preventDefault(); generate(false); });
  byId('refresh-tts').addEventListener('click', () => generate(true));
  audioButton.addEventListener('click', generateAudio);
  byId('perform-poetry').addEventListener('click', performPoetry);
  byId('cancel-performance').addEventListener('click', cancelPerformance);
  byId('check-poetry').addEventListener('click', () => { clearTimeout(checkTimer); check(); });
  byId('copy-poem').addEventListener('click', () => copy(poemText(), '诗词'));
  byId('copy-tts').addEventListener('click', () => copy(prompt.value, '提示词'));
  byId('download-poem').addEventListener('click', () => download(poemText(), ''));
  byId('download-tts').addEventListener('click', () => download(prompt.value, '-TTS提示词'));
  fields.forEach((field) => field.addEventListener('input', () => {
    update();
    save();
    scheduleCheck();
  }));
  kindRadios.forEach((radio) => radio.addEventListener('change', () => {
    sourceNote.textContent = read().kind === 'original' ? '原创诗词。' : '已有诗词，请核对作者与朝代。';
    update();
    save();
    scheduleCheck();
  }));
  window.addEventListener('poetry-config-updated', update);

  try {
    const saved = JSON.parse(localStorage.getItem(storage)) || {};
    fields.forEach((field) => {
      const value = saved.fields?.[field.id];
      if (typeof value === 'string' && (field !== poetryForm || [...poetryForm.options].some((option) => option.value === value))) {
        field.value = value;
      }
    });
    if (kindRadios.some((radio) => radio.value === saved.kind)) {
      kindRadios.forEach((radio) => { radio.checked = radio.value === saved.kind; });
    }
    if (typeof saved.note === 'string') sourceNote.textContent = saved.note;
    if (saved.fingerprint) {
      const baseline = JSON.parse(saved.fingerprint);
      baseline.form ??= 'auto';
      baseline.cipai ??= '';
      const keys = Object.keys(JSON.parse(currentFingerprint()));
      fingerprint = JSON.stringify(Object.fromEntries(keys.map((key) => [key, baseline[key]])));
    }
  } catch {
    // Keep the form defaults if saved browser data cannot be read.
  }
  update();
  scheduleCheck();
  watchPerformance();
})();
