const $ = (selector) => document.querySelector(selector);
let chatHistory = [];
let selectedChatImage = null;
let modelHasSavedKey = false;

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  document.body.append(el);
  setTimeout(() => el.remove(), 3500);
}

document.querySelectorAll('.nav').forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll('.nav,.tab-panel').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    $('#' + button.dataset.tab).classList.add('active');
    $('#page-title').textContent = { ask: 'AI 对话', image: '以图搜图', manage: '资料同步' }[button.dataset.tab];
    if (button.dataset.tab === 'manage') loadStatus();
  };
});

$('#open-model-settings').onclick = async () => {
  await loadModelConfig();
  $('#model-dialog').showModal();
};
$('#close-model-settings').onclick = () => $('#model-dialog').close();
$('#model-dialog').onclick = (event) => {
  if (event.target === $('#model-dialog')) $('#model-dialog').close();
};

$('#chat-image').onchange = (event) => {
  selectedChatImage = event.target.files[0] || null;
  renderAttachment();
};

function renderAttachment() {
  const holder = $('#chat-attachment');
  if (!selectedChatImage) {
    holder.hidden = true;
    holder.innerHTML = '';
    return;
  }
  holder.hidden = false;
  const preview = URL.createObjectURL(selectedChatImage);
  holder.innerHTML = `<img src="${preview}" alt="待发送图片"><span>${esc(selectedChatImage.name)}</span><button type="button" aria-label="移除图片">×</button>`;
  holder.querySelector('button').onclick = () => {
    selectedChatImage = null;
    $('#chat-image').value = '';
    renderAttachment();
  };
}

function addMessage(role, content, imageUrl = '') {
  const bubble = document.createElement('article');
  bubble.className = `chat-message ${role}`;
  if (role === 'assistant') {
    bubble.innerHTML = `<div class="assistant-mark">✧</div><div class="message-body"><div class="message-text">${esc(content)}</div></div>`;
  } else {
    bubble.innerHTML = `<div class="message-body">${imageUrl ? `<img class="user-image" src="${imageUrl}" alt="已发送图片">` : ''}<div class="message-text">${esc(content)}</div></div>`;
  }
  $('#chat-thread').append(bubble);
  bubble.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  return bubble;
}

function renderReferences(bubble, data) {
  const body = bubble.querySelector('.message-body');
  const sources = data.sources || [];
  const images = data.image_matches || [];
  if (data.used_ai && data.model) {
    const model = document.createElement('div');
    model.className = 'model-caption';
    model.textContent = `AI 回答 · ${data.model}`;
    body.append(model);
  } else if (sources.length || images.length) {
    const model = document.createElement('div');
    model.className = 'model-caption';
    model.textContent = '本地资料检索';
    body.append(model);
  }
  if (sources.length) {
    const section = document.createElement('div');
    section.className = 'reference-list';
    section.innerHTML = `<b>参考资料 (${sources.length})</b>` + sources.map((item) => {
      const page = item.page == null ? '' : ` · 第 ${item.page} 页`;
      return `<a class="reference-item" href="/api/files/${item.file_id}/open" target="_blank"><span>▤</span><span><b>${esc(item.name)}</b><small>${esc(item.path)}${page}</small>${item.snippet ? `<em>${esc(item.snippet.slice(0, 180))}</em>` : ''}</span><i>↗</i></a>`;
    }).join('');
    body.append(section);
  }
  if (images.length) {
    const section = document.createElement('div');
    section.className = 'chat-image-matches';
    section.innerHTML = `<b>相似产品图片 (${images.length})</b><div class="chat-match-grid">${images.map((item) => `<a href="/api/files/${item.file_id}/open" target="_blank"><img src="${item.preview_url}" loading="lazy" alt="${esc(item.name)}"><span>${esc(item.name)}</span></a>`).join('')}</div>`;
    body.append(section);
  }
}

$('#chat-form').onsubmit = async (event) => {
  event.preventDefault();
  const message = $('#chat-message').value.trim();
  const image = selectedChatImage;
  if (!message && !image) {
    toast('请输入问题或附上一张产品图片');
    return;
  }

  const imageUrl = image ? URL.createObjectURL(image) : '';
  const userText = message || '请识别这张图片，并查找资料库中的相关产品信息。';
  addMessage('user', userText, imageUrl);
  const pending = addMessage('assistant', '正在检索产品资料…');
  pending.classList.add('pending');
  $('#chat-send').disabled = true;
  const form = new FormData();
  form.append('message', message);
  form.append('history', JSON.stringify(chatHistory.slice(-8)));
  form.append('model_id', $('#chat-model-select').value || 'local');
  if (image) form.append('image', image);

  try {
    const response = await fetch('/api/assistant/chat', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '对话请求失败');
    pending.remove();
    const reply = addMessage('assistant', data.answer || '没有生成回答。');
    renderReferences(reply, data);
    chatHistory.push({ role: 'user', content: userText }, { role: 'assistant', content: data.answer || '' });
    chatHistory = chatHistory.slice(-12);
    $('#chat-message').value = '';
    selectedChatImage = null;
    $('#chat-image').value = '';
    renderAttachment();
  } catch (error) {
    pending.remove();
    addMessage('assistant', error.message || '暂时无法回答，请检查模型设置和服务状态。');
  } finally {
    $('#chat-send').disabled = false;
    $('#chat-message').focus();
  }
};

$('#chat-message').onkeydown = (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('#chat-form').requestSubmit();
  }
};

const modelDefaults = {
  siliconflow: ['https://api.siliconflow.cn/v1', 'deepseek-ai/DeepSeek-V4.1-Flash'],
  deepseek: ['https://api.deepseek.com/v1', 'deepseek-chat'],
  openai: ['https://api.openai.com/v1', 'gpt-4o-mini'],
  ollama: ['http://host.docker.internal:11434/v1', 'qwen2.5:7b'],
};
$('#model-form [name=provider]').onchange = (event) => {
  const preset = modelDefaults[event.target.value];
  if (preset) {
    $('#model-form [name=base_url]').value = preset[0];
    $('#model-form [name=model]').value = preset[1];
  }
};

async function loadModelConfig() {
  try {
    const result = await fetch('/api/config/models').then((response) => response.json());
    const select = $('#model-profile-select');
    select.innerHTML = result.profiles.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}${item.has_api_key ? '' : '（未配置 Key）'}</option>`).join('');
    const selectedId = result.profiles.some((item) => item.id === result.active_profile_id) ? result.active_profile_id : result.profiles[0]?.id;
    if (!selectedId) return;
    select.value = selectedId;
    await loadModelProfile(selectedId);
    await loadChatModels();
  } catch (_) { /* The page remains usable if settings are unavailable. */ }
}

async function loadModelProfile(profileId) {
  if (!profileId || profileId === 'new') return;
  try {
    const config = await fetch(`/api/config/model?profile_id=${encodeURIComponent(profileId)}`).then((response) => response.json());
    modelHasSavedKey = config.has_api_key;
    $('#model-form [name=profile_id]').value = config.profile_id;
    $('#model-form [name=profile_name]').value = config.name || '';
    $('#model-form [name=provider]').value = config.provider;
    $('#model-form [name=base_url]').value = config.base_url;
    $('#model-form [name=model]').value = config.model;
    $('#model-form [name=api_key]').value = '';
    $('#model-form [name=api_key]').placeholder = config.has_api_key ? '已保存密钥；留空保持不变' : '请输入 API Key（Ollama 可留空）';
    $('#model-form [name=clear_api_key]').checked = false;
    $('#delete-model-profile').disabled = false;
  } catch (_) { /* Keep the current form if a profile was removed elsewhere. */ }
}

$('#model-profile-select').onchange = (event) => loadModelProfile(event.target.value);

$('#add-model-profile').onclick = () => {
  const select = $('#model-profile-select');
  if (!select.querySelector('option[value="new"]')) select.add(new Option('新模型配置（未保存）', 'new'));
  select.value = 'new';
  modelHasSavedKey = false;
  $('#model-form').reset();
  $('#model-form [name=profile_id]').value = 'new';
  $('#model-form [name=profile_name]').value = '';
  $('#model-form [name=api_key]').value = '';
  $('#model-form [name=api_key]').placeholder = '请输入 API Key';
  $('#model-form [name=clear_api_key]').checked = false;
  $('#model-form [name=provider]').value = 'siliconflow';
  $('#model-form [name=base_url]').value = modelDefaults.siliconflow[0];
  $('#model-form [name=model]').value = modelDefaults.siliconflow[1];
  $('#delete-model-profile').disabled = true;
  $('#model-state').textContent = '';
  $('#model-form [name=profile_name]').focus();
};

$('#delete-model-profile').onclick = async () => {
  const profileId = $('#model-form [name=profile_id]').value;
  if (!profileId || profileId === 'new') return;
  if (!window.confirm('删除这条模型配置？已保存的 API Key 也会从本机配置中移除。')) return;
  try {
    const response = await fetch(`/api/config/model/${encodeURIComponent(profileId)}`, { method: 'DELETE' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '删除失败');
    localStorage.removeItem('chat-model-id');
    toast('模型配置已删除');
    await loadModelConfig();
    await loadStatus();
  } catch (error) {
    $('#model-state').textContent = error.message || '删除失败';
  }
};

async function loadChatModels() {
  try {
    const result = await fetch('/api/config/models').then((response) => response.json());
    const select = $('#chat-model-select');
    const remembered = localStorage.getItem('chat-model-id') || '';
    select.innerHTML = '<option value="local">仅本地检索（不调用 AI）</option>' + result.profiles.map((item) => {
      const missingKey = !item.has_api_key && item.provider !== 'ollama';
      return `<option value="${esc(item.id)}" ${missingKey ? 'disabled' : ''}>${esc(item.name)}${missingKey ? ' · 未配置 Key' : ''}</option>`;
    }).join('');
    const selected = result.profiles.find((item) => item.id === remembered && (item.has_api_key || item.provider === 'ollama'));
    const active = result.profiles.find((item) => item.id === result.active_profile_id && (item.has_api_key || item.provider === 'ollama'));
    select.value = selected?.id || active?.id || 'local';
    localStorage.setItem('chat-model-id', select.value);
  } catch (_) { /* Local search remains available if model settings cannot be loaded. */ }
}

$('#chat-model-select').onchange = (event) => localStorage.setItem('chat-model-id', event.target.value);

$('#model-form').onsubmit = async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  values.clear_api_key = $('#model-form [name=clear_api_key]').checked;
  $('#model-state').textContent = '保存中…';
  try {
    const response = await fetch('/api/config/model', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail);
    $('#model-form [name=api_key]').value = '';
    $('#model-form [name=clear_api_key]').checked = false;
    $('#model-form [name=profile_id]').value = data.profile_id;
    $('#model-form [name=profile_name]').value = data.name;
    modelHasSavedKey = data.has_api_key;
    $('#model-form [name=api_key]').placeholder = modelHasSavedKey ? '已保存密钥；留空保持不变' : '请输入 API Key（Ollama 可留空）';
    $('#model-state').textContent = '已保存';
    toast('模型设置已保存');
    await loadModelConfig();
    $('#model-profile-select').value = data.profile_id;
    await loadModelProfile(data.profile_id);
    $('#chat-model-select').value = data.profile_id;
    localStorage.setItem('chat-model-id', data.profile_id);
    await loadStatus();
  } catch (error) {
    $('#model-state').textContent = error.message || '保存失败';
  }
};

$('#test-model').onclick = async () => {
  const state = $('#model-state');
  const provider = $('#model-form [name=provider]').value;
  const profileId = $('#model-form [name=profile_id]').value;
  if (profileId === 'new') {
    state.textContent = '先保存这条模型配置，再测试连接。';
    return;
  }
  if ($('#model-form [name=api_key]').value.trim()) {
    state.textContent = '请先点“保存设置”，再测试连接。';
    return;
  }
  if (!modelHasSavedKey && provider !== 'ollama') {
    state.textContent = '尚未保存 API Key。请先填写并保存，再测试连接。';
    return;
  }
  state.textContent = '连接测试中…';
  try {
    const response = await fetch('/api/config/model/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile_id: profileId }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail);
    state.textContent = `${data.message} · ${data.model}`;
  } catch (error) {
    state.textContent = error.message || '测试失败';
  }
};

$('#image-form [name=image]').onchange = (event) => {
  const file = event.target.files[0];
  if (file) {
    const preview = $('#query-preview');
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
  }
};

$('#image-form').onsubmit = async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  $('#image-head').hidden = false;
  $('#image-meta').textContent = '正在匹配…';
  $('#image-results').innerHTML = '';
  try {
    const response = await fetch('/api/search/image', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail);
    $('#image-meta').textContent = `${data.results.length} 个候选 · ${data.method}`;
    $('#image-results').innerHTML = data.results.map((item) => `<article class="image-card"><img src="${item.preview_url}" loading="lazy"><div><b title="${esc(item.path)}">${esc(item.name)}</b><span>相似度 ${(item.similarity * 100).toFixed(1)}%</span><a href="/api/files/${item.file_id}/open" target="_blank">打开原图 →</a></div></article>`).join('') || '<p class="file-path">图库中暂时没有可匹配的图片，请先同步资料。</p>';
  } catch (error) {
    toast(error.message || '图片搜索失败');
  }
};

$('#sync-btn').onclick = async () => {
  const button = $('#sync-btn');
  button.disabled = true;
  button.innerHTML = '正在同步…';
  try {
    const response = await fetch('/api/index/sync', { method: 'POST' });
    const data = await response.json();
    $('#sync-log').innerHTML = `<b>同步完成</b><pre>扫描文件：${data.scanned}\n新增/更新：${data.indexed}\n未变化：${data.unchanged}\n已移除：${data.removed}\n失败：${data.errors.length}${data.errors.map((item) => `\n- ${item.path}: ${item.error}`).join('')}</pre>`;
    toast('资料索引已更新');
    await loadStatus();
  } catch (_) {
    toast('同步失败');
  } finally {
    button.disabled = false;
    button.innerHTML = '立即同步 <span>↻</span>';
  }
};

$('#material-upload').onchange = async (event) => {
  const files = [...event.target.files];
  if (!files.length) return;
  const form = new FormData();
  files.forEach((file) => form.append('files', file));
  toast(`正在上传 ${files.length} 个文件…`);
  try {
    const response = await fetch('/api/files/upload', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail);
    $('#sync-log').innerHTML = `<b>上传完成</b><pre>${data.files.map((item) => `${item.status}: ${item.path || item.name}${item.error ? ` · ${item.error}` : ''}`).join('\n')}</pre>`;
    await loadStatus();
  } catch (error) {
    toast(error.message || '上传失败');
  } finally {
    event.target.value = '';
  }
};

async function loadStatus() {
  try {
    const response = await fetch('/api/status');
    const data = await response.json();
    $('#side-count').textContent = `${data.total_files} 个文件`;
    $('#library-path').textContent = data.library_dir;
    $('#total-files').textContent = data.total_files;
    $('#error-files').textContent = data.errors;
    $('#last-indexed').textContent = data.last_indexed ? new Date(data.last_indexed).toLocaleString() : '暂无';
    $('#chat-model-status').textContent = data.configured_model_count ? `${data.configured_model_count} 个模型可用` : 'AI 尚未配置';
  } catch (_) { /* Keep the static shell available while offline. */ }
}

function esc(value = '') {
  return String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

loadStatus();
loadChatModels();
