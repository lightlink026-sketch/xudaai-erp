const $ = (selector) => document.querySelector(selector);
let chatHistory = [];
let selectedChatImage = null;
let modelHasSavedKey = false;
const quoteCandidates = new Map();
const selectedQuoteProducts = new Map();

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
    $('#page-title').textContent = { ask: 'AI 对话', manage: '资料同步' }[button.dataset.tab];
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
$('#image-threshold').oninput = (event) => {
  $('#image-threshold-value').value = Number(event.target.value).toFixed(2);
};

function renderAttachment() {
  const holder = $('#chat-attachment');
  if (!selectedChatImage) {
    holder.hidden = true;
    holder.innerHTML = '';
    $('#chat-image-options').hidden = true;
    return;
  }
  holder.hidden = false;
  $('#chat-image-options').hidden = false;
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
      quoteCandidates.set(String(item.file_id), item);
      const page = item.page == null ? '' : ` · 第 ${item.page} 页`;
      return `<div class="reference-select-row"><label><input type="checkbox" data-select-quote="${item.file_id}" ${selectedQuoteProducts.has(String(item.file_id)) ? 'checked' : ''}><span>加入单据</span></label><a class="reference-item" href="/api/files/${item.file_id}/open" target="_blank"><span>▤</span><span><b>${esc(item.name)}</b><small>${esc(item.path)}${page}</small>${item.snippet ? `<em>${esc(item.snippet.slice(0, 180))}</em>` : ''}</span><i>↗</i></a></div>`;
    }).join('');
    body.append(section);
  }
  if (images.length) {
    const section = document.createElement('div');
    section.className = 'chat-image-matches';
    section.innerHTML = `<b>相似产品图片 (${images.length})</b><div class="chat-match-grid">${images.map((item) => { quoteCandidates.set(String(item.file_id), item); return `<div class="chat-match-card"><a href="/api/files/${item.file_id}/open" target="_blank"><img src="${item.preview_url}" loading="lazy" alt="${esc(item.name)}"><span>${esc(item.name)}</span><small>相似度 ${Number(item.similarity).toFixed(3)}</small></a><label><input type="checkbox" data-select-quote="${item.file_id}" ${selectedQuoteProducts.has(String(item.file_id)) ? 'checked' : ''}>加入报价</label></div>`; }).join('')}</div>`;
    body.append(section);
  }
}

$('#chat-thread').addEventListener('change', (event) => {
  const checkbox = event.target.closest('[data-select-quote]');
  if (!checkbox) return;
  const id = String(checkbox.dataset.selectQuote);
  if (checkbox.checked && !selectedQuoteProducts.has(id) && selectedQuoteProducts.size >= 20) {
    checkbox.checked = false;
    toast('一次最多选择 20 个产品，请分批生成报价单');
    return;
  }
  if (checkbox.checked) selectedQuoteProducts.set(id, quoteCandidates.get(id));
  else selectedQuoteProducts.delete(id);
  document.querySelectorAll(`[data-select-quote="${CSS.escape(id)}"]`).forEach((other) => { other.checked = checkbox.checked; });
  $('#open-quote-dialog').textContent = selectedQuoteProducts.size ? `报价 / PI (${selectedQuoteProducts.size})` : '报价 / PI';
  const holder = $('#quote-selected-products');
  if (holder) renderSelectedQuoteProducts();
});

function renderSelectedQuoteProducts() {
  const holder = $('#quote-selected-products');
  if (!holder) return;
  const selected = [...selectedQuoteProducts.values()];
  holder.innerHTML = selected.length ? `<b>已勾选 ${selected.length} 项检索结果</b>${selected.map((item) => `<span>${esc(item.name)}<small>${esc(item.path || '')}</small><button type="button" data-remove-selected="${item.file_id}" aria-label="移除">移除</button></span>`).join('')}` : '<span>还没有勾选产品。请先在对话结果中勾选产品图片或资料。</span>';
}

$('#quote-selected-products').addEventListener('click', (event) => {
  const button = event.target.closest('[data-remove-selected]');
  if (!button) return;
  const id = String(button.dataset.removeSelected);
  selectedQuoteProducts.delete(id);
  document.querySelectorAll(`[data-select-quote="${CSS.escape(id)}"]`).forEach((checkbox) => { checkbox.checked = false; });
  $('#open-quote-dialog').textContent = selectedQuoteProducts.size ? `报价 / PI (${selectedQuoteProducts.size})` : '报价 / PI';
  renderSelectedQuoteProducts();
});

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
  // Clear the composer as soon as the user sends, even if the model request fails.
  $('#chat-message').value = '';
  selectedChatImage = null;
  $('#chat-image').value = '';
  renderAttachment();
  addMessage('user', userText, imageUrl);
  const pending = addMessage('assistant', '正在检索产品资料…');
  pending.classList.add('pending');
  $('#chat-send').disabled = true;
  const form = new FormData();
  form.append('message', message);
  form.append('history', JSON.stringify(chatHistory.slice(-8)));
  form.append('model_id', $('#chat-model-select').value || 'local');
  form.append('image_threshold', $('#image-threshold').value || '0.20');
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

let activeQuoteId = null;
let activeQuoteStatus = 'draft';
let pendingDocumentType = 'quote';
let quoteDefaults = {};
let autoImportedSelection = '';
let availableCompanyProfiles = [];

async function loadQuoteTemplates() {
  const select = $('#quote-template');
  const current = select.value || '';
  try {
    const [templateResponse, defaultsResponse] = await Promise.all([fetch('/api/quotes/templates'), fetch('/api/quotes/defaults')]);
    const data = await templateResponse.json();
    quoteDefaults = (await defaultsResponse.json()).defaults || {};
    select.innerHTML = '<option value="">系统默认模板</option>' + (data.templates || []).map((item) => `<option value="${esc(item.name)}">${esc(item.name)}</option>`).join('');
    select.value = current || quoteDefaults.template_name || '';
  } catch (_) { select.innerHTML = '<option value="">系统默认模板</option>'; select.value = ''; }
}

async function loadQuoteCustomers() {
  const select = $('#quote-customer-select');
  try {
    const response = await fetch('/api/quotes/customers'); const data = await response.json();
    select.innerHTML = '<option value="">新客户 / 手动填写</option>' + (data.customers || []).map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('');
    select._customers = data.customers || [];
  } catch (_) { select.innerHTML = '<option value="">新客户 / 手动填写</option>'; select._customers = []; }
}

function applyCompanyProfile(profile, preferDefaults = false) {
  if (!profile) return;
  const value = (key) => preferDefaults && quoteDefaults[key] ? quoteDefaults[key] : profile[key] || '';
  const form = $('#quote-form');
  const english = form.elements.namedItem('language').value === 'en';
  form.elements.namedItem('company_name').value = (preferDefaults && quoteDefaults.company_name) ? quoteDefaults.company_name : ((english && profile.name_en) || profile.name || '');
  form.elements.namedItem('company_address').value = (preferDefaults && quoteDefaults.company_address) ? quoteDefaults.company_address : ((english && profile.address_en) || profile.address || '');
  form.elements.namedItem('company_phone').value = value('company_phone');
  form.elements.namedItem('company_email').value = value('company_email');
  const source = profile.sources ? `已载入公司资料：${profile.sources}` : `已载入公司资料：${profile.name}`;
  $('#quote-state').textContent = source;
}

async function loadCompanyProfiles() {
  const select = $('#quote-company-profile');
  try {
    const response = await fetch('/api/quotes/companies');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '公司资料读取失败');
    availableCompanyProfiles = data.companies || [];
    select.innerHTML = '<option value="">手动填写</option>' + availableCompanyProfiles.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}${item.phone ? ` · ${esc(item.phone)}` : ''}</option>`).join('');
    const remembered = quoteDefaults.company_profile_id || data.selected_profile_id || '';
    const profileId = availableCompanyProfiles.some((item) => item.id === remembered) ? remembered : (availableCompanyProfiles.length === 1 ? availableCompanyProfiles[0].id : '');
    select.value = profileId;
    if (!activeQuoteId && profileId) applyCompanyProfile(availableCompanyProfiles.find((item) => item.id === profileId), Boolean(remembered));
  } catch (error) {
    availableCompanyProfiles = [];
    select.innerHTML = '<option value="">手动填写</option>';
    if (error) console.warn(error);
  }
}

$('#open-quote-dialog').onclick = async () => {
  await loadQuoteTemplates();
  await Promise.all([loadQuoteCustomers(), loadQuotes(), loadCompanyProfiles()]);
  if (!activeQuoteId) $('#quote-form').elements.namedItem('document_type').value = pendingDocumentType;
  renderSelectedQuoteProducts();
  $('#quote-dialog').showModal();
  const signature = [...selectedQuoteProducts.keys()].sort().join(',');
  if (!activeQuoteId && signature && signature !== autoImportedSelection) await importSelectedQuoteProducts(true);
};
$('#close-quote-dialog').onclick = () => $('#quote-dialog').close();
$('#quote-dialog').onclick = (event) => { if (event.target === $('#quote-dialog')) $('#quote-dialog').close(); };
$('#start-quote').onclick = () => {
  pendingDocumentType = 'quote'; autoImportedSelection = ''; fillQuoteForm({ document_type: 'quote' });
  $('#open-quote-dialog').click();
};
$('#start-pi').onclick = () => {
  pendingDocumentType = 'pi'; autoImportedSelection = ''; fillQuoteForm({ document_type: 'pi' });
  $('#open-quote-dialog').click();
};

$('#quote-template-upload').onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const form = new FormData(); form.append('file', file);
  try {
    const response = await fetch('/api/quotes/templates', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '模板上传失败');
    await loadQuoteTemplates(); $('#quote-template').value = data.name;
    $('#quote-state').textContent = `已上传并选用模板：${data.name}`;
  } catch (error) { $('#quote-state').textContent = error.message; }
  finally { event.target.value = ''; }
};

function addQuoteItem(item = {}) {
  const row = document.createElement('div');
  row.className = 'quote-item-row';
  const imageFileId = Number(item.image_file_id) || 0;
  const imagePreview = imageFileId > 0 ? `<img src="/api/files/${imageFileId}/preview" alt="${esc(item.name || '产品图片')}" loading="lazy">` : '<span>无图片</span>';
  row.innerHTML = `<div class="quote-product-photo">${imagePreview}<input type="hidden" data-field="image_path" value="${esc(item.image_path || '')}"><input type="hidden" data-field="image_file_id" value="${imageFileId}"></div><div class="quote-item-fields"><label>产品名称/型号<input data-field="name" required value="${esc(item.name || '')}" placeholder="产品型号"></label><label>描述<input data-field="description" value="${esc(item.description || '')}" placeholder="产品描述"></label><label>颜色<input data-field="colors" value="${esc(item.colors || '')}" placeholder="黑色 / 卡其色（不填数量）"></label><label>尺码配比<input data-field="size_breakdown" value="${esc(item.size_breakdown || '')}" placeholder="36:2, 37:4, 38:6…"></label><label>其他配置<input data-field="configuration" value="${esc(item.configuration || '')}" placeholder="其他规格说明"></label><label>箱规<input data-field="carton_spec" value="${esc(item.carton_spec || '')}" placeholder="75 × 53 × 35 cm"></label><label>单箱体积<input data-field="carton_volume" value="${esc(item.carton_volume || '')}" placeholder="0.15 m³/箱"></label><label>毛重<input data-field="gross_weight" value="${esc(item.gross_weight || '')}" placeholder="kg/箱"></label><label>净重<input data-field="net_weight" value="${esc(item.net_weight || '')}" placeholder="kg/箱"></label><label>总数量<input data-field="quantity" type="number" min="0.001" step="0.001" value="${esc(item.quantity || '1')}"></label><label>MOQ<input data-field="moq" type="number" min="0" step="1" value="${esc(item.moq || '0')}"></label><label>单位<input data-field="unit" value="${esc(item.unit || 'pcs')}"></label><label>单价<input data-field="unit_price" type="number" min="0" step="0.01" value="${esc(item.unit_price || '0')}"></label><label>折扣 %<input data-field="discount_percent" type="number" min="0" max="100" step="0.01" value="${esc(item.discount_percent || '0')}"></label><label>价格/资料来源<input data-field="source" value="${esc(item.source || '人工确认')}" placeholder="文件名、工作表或人工确认"></label></div><button type="button" class="quote-remove" aria-label="删除产品行">删除</button>`;
  row.querySelectorAll('input').forEach((input) => input.addEventListener('input', updateQuoteTotal));
  row.querySelector('.quote-remove').onclick = () => { row.remove(); updateQuoteTotal(); };
  $('#quote-items').append(row);
  updateQuoteTotal();
}

function quotePayload() {
  const form = $('#quote-form');
  const value = (name) => form.elements.namedItem(name).value.trim();
  const selectValue = (name, customName) => value(name) === 'custom' ? value(customName) : value(name);
  const quoteDate = value('quote_date');
  const expiry = new Date(`${quoteDate}T12:00:00`);
  const daysRaw = value('valid_days') === 'custom' ? value('valid_days_custom') : value('valid_days');
  expiry.setDate(expiry.getDate() + Math.min(365, Math.max(1, Number(daysRaw) || 30)));
  return {
    document_type: value('document_type'), language: value('language'), template_name: value('template_name'),
    company_profile_id: $('#quote-company-profile').value, company_name: value('company_name'), company_address: value('company_address'), company_phone: value('company_phone'), company_email: value('company_email'),
    company_contact: [value('company_address'), value('company_phone'), value('company_email')].filter(Boolean).join('\n'),
    customer: value('customer'), customer_address: value('customer_address'),
    quote_date: quoteDate, valid_until: Number.isNaN(expiry.getTime()) ? '' : expiry.toISOString().slice(0,10), currency: value('currency'),
    exchange_rate: value('exchange_rate'), exchange_date: value('exchange_date'), origin_port: value('origin_port'), destination_port: value('destination_port'), insurance_term: value('insurance_term'),
    incoterm: selectValue('incoterm','incoterm_custom'), payment_terms: selectValue('payment_terms','payment_terms_custom'), payment_note: value('payment_note'), shipping_term: selectValue('shipping_term','shipping_term_custom'), tax_term: selectValue('tax_term','tax_term_custom'), tax_amount: value('tax_amount') || '0', tax_note: value('tax_note'), delivery_time: value('delivery_time'), bank_details: value('bank_details'), notes: value('notes'),
    shipping: value('shipping') || '0', other_fees: value('other_fees') || '0',
    items: [...document.querySelectorAll('.quote-item-row')].map((row) => {
      const item = {};
      row.querySelectorAll('[data-field]').forEach((input) => { item[input.dataset.field] = input.value.trim(); });
      return item;
    }),
  };
}

function updateQuoteTotal() {
  const payload = quotePayload();
  const places = payload.currency === 'JPY' ? 0 : 2;
  const subtotal = payload.items.reduce((sum, item) => {
    const qty = Number(item.quantity) || 0;
    const price = Number(item.unit_price) || 0;
    const discount = Math.min(100, Math.max(0, Number(item.discount_percent) || 0));
    return sum + qty * price * (1 - discount / 100);
  }, 0);
  const total = subtotal + (Number(payload.shipping) || 0) + (Number(payload.other_fees) || 0) + (Number(payload.tax_amount) || 0);
  $('#quote-total').textContent = `总计：${payload.currency} ${total.toFixed(places)}`;
  const belowMoq = payload.items.filter((item) => Number(item.moq) > 0 && Number(item.quantity) < Number(item.moq));
  if (belowMoq.length) $('#quote-state').textContent = `MOQ 未达标：${belowMoq.map((item) => item.name).join('、')}。请核对起订量。`;
}

function fillQuoteForm(data, id = null, quoteNo = '') {
  const form = $('#quote-form');
  data = { ...quoteDefaults, ...data };
  if (!data.company_address && data.company_contact) data.company_address = data.company_contact;
  if (data.document_type === 'PI') data.document_type = 'pi';
  if (data.document_type === 'Q') data.document_type = 'quote';
  data.document_type ||= 'quote'; data.language ||= 'zh'; data.incoterm ||= 'EXW'; data.payment_terms ||= 'T/T 30% deposit, 70% before shipment'; data.insurance_term ||= 'Buyer';
  data.shipping_term ||= 'Excluded'; data.tax_term ||= 'Excluded';
  for (const name of ['document_type','language','template_name','company_name','company_address','company_phone','company_email','customer','customer_address','quote_date','currency','exchange_rate','exchange_date','origin_port','destination_port','insurance_term','delivery_time','bank_details','notes','shipping','other_fees','shipping_term','tax_term','tax_amount','tax_note','payment_note']) {
    form.elements.namedItem(name).value = data[name] ?? (name === 'quote_date' ? new Date().toISOString().slice(0,10) : ['shipping','other_fees'].includes(name) ? '0' : name === 'currency' ? 'USD' : '');
  }
  for (const [selectName, customName] of [['incoterm','incoterm_custom'],['payment_terms','payment_terms_custom'],['shipping_term','shipping_term_custom'],['tax_term','tax_term_custom']]) {
    const select = form.elements.namedItem(selectName); const raw = data[selectName] || '';
    if ([...select.options].some((option) => option.value === raw)) { select.value = raw; form.elements.namedItem(customName).value = ''; form.elements.namedItem(customName).hidden = true; }
    else { select.value = 'custom'; form.elements.namedItem(customName).value = raw; form.elements.namedItem(customName).hidden = false; }
  }
  const expiryDays = data.valid_until && data.quote_date ? Math.round((new Date(`${data.valid_until}T12:00:00`) - new Date(`${data.quote_date}T12:00:00`)) / 86400000) : 30;
  const validSelect = $('#quote-valid-days');
  const allowedDays = ['7','15','30','60','90'];
  if (allowedDays.includes(String(expiryDays))) { validSelect.value = String(expiryDays); $('#quote-form').elements.namedItem('valid_days_custom').hidden = true; }
  else { validSelect.value = 'custom'; $('#quote-form').elements.namedItem('valid_days_custom').value = Math.min(365, Math.max(1, expiryDays || 30)); $('#quote-form').elements.namedItem('valid_days_custom').hidden = false; }
  $('#quote-items').innerHTML = '';
  (data.items?.length ? data.items : [{}]).forEach(addQuoteItem);
  activeQuoteId = id;
  activeQuoteStatus = 'draft';
  $('#quote-number').textContent = quoteNo;
  $('#quote-form-title').textContent = id ? '编辑报价草稿' : '新建报价';
  $('#quote-state').textContent = '产品价格请在生成前核对。';
  $('#quote-save').disabled = false;
  $('#quote-pdf').disabled = false;
  showQuoteStage(1);
  updateQuoteTotal();
}

let activeQuoteStage = 1;
function showQuoteStage(stage) {
  activeQuoteStage = Math.max(1, Math.min(3, stage));
  document.querySelectorAll('[data-quote-stage]').forEach((panel) => { panel.hidden = Number(panel.dataset.quoteStage) !== activeQuoteStage; });
  document.querySelectorAll('[data-step-go]').forEach((button) => button.classList.toggle('active', Number(button.dataset.stepGo) === activeQuoteStage));
  $('#quote-stage-label').textContent = `第 ${activeQuoteStage} 步，共 3 步`;
  $('#quote-prev').hidden = activeQuoteStage === 1;
  $('#quote-next').hidden = activeQuoteStage === 3;
  $('#quote-import-chat').hidden = activeQuoteStage !== 1;
  $('#quote-save').hidden = activeQuoteStage !== 3;
  $('#quote-pdf').hidden = activeQuoteStage !== 3;
  $('#quote-save-defaults').hidden = activeQuoteStage !== 3;
}

function validateQuoteStage(stage) {
  if (stage === 1) {
    const rows = [...document.querySelectorAll('.quote-item-row')];
    if (!rows.length || rows.some((row) => !row.querySelector('[data-field="name"]').value.trim())) return '请先加入至少一个产品并填写型号。';
  }
  if (stage === 2 && !$('#quote-form').elements.namedItem('customer').value.trim()) return '请选择常用客户或填写客户名称。';
  return '';
}
document.querySelectorAll('[data-step-go]').forEach((button) => button.onclick = () => {
  const target = Number(button.dataset.stepGo);
  for (let stage = activeQuoteStage; stage < target; stage += 1) {
    const error = validateQuoteStage(stage);
    if (error) { $('#quote-state').textContent = error; return; }
  }
  showQuoteStage(target);
});
$('#quote-prev').onclick = () => showQuoteStage(activeQuoteStage - 1);
$('#quote-next').onclick = () => {
  const error = validateQuoteStage(activeQuoteStage);
  if (error) { $('#quote-state').textContent = error; return; }
  $('#quote-state').textContent = '';
  showQuoteStage(activeQuoteStage + 1);
};

$('#quote-customer-select').onchange = (event) => {
  const customer = event.target._customers?.find((item) => item.id === event.target.value);
  if (customer) { $('#quote-form').elements.namedItem('customer').value = customer.name; $('#quote-form').elements.namedItem('customer_address').value = customer.address || ''; }
};

$('#quote-company-profile').onchange = (event) => {
  const profile = availableCompanyProfiles.find((item) => item.id === event.target.value);
  if (profile) applyCompanyProfile(profile);
};

$('#quote-form').addEventListener('change', (event) => {
  const name = event.target.name;
  if (name === 'valid_days') $('#quote-form').elements.namedItem('valid_days_custom').hidden = event.target.value !== 'custom';
  if (['incoterm','payment_terms','shipping_term','tax_term'].includes(name)) {
    const custom = $('#quote-form').elements.namedItem(`${name}_custom`);
    custom.hidden = event.target.value !== 'custom';
    if (!custom.hidden) custom.focus();
  }
});

async function saveQuote() {
  const payload = quotePayload();
  const response = await fetch(activeQuoteId ? `/api/quotes/${activeQuoteId}` : '/api/quotes', {
    method: activeQuoteId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '保存报价失败');
  activeQuoteId = data.id;
  $('#quote-number').textContent = data.quote_no;
  $('#quote-form-title').textContent = '编辑报价草稿';
  await loadQuotes();
  return data;
}

async function loadQuotes() {
  const holder = $('#quote-list');
  if (!holder) return;
  try {
    const response = await fetch('/api/quotes');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法读取报价记录');
    holder.innerHTML = data.quotes.length ? `<div class="quote-list">${data.quotes.map((quote) => `<div class="quote-list-row"><span><b>${esc(quote.quote_no)}</b><small>${esc(quote.customer || '未填写客户')} · ${esc(quote.currency)} ${esc(quote.total)} · ${quote.document_type === 'pi' ? 'PI' : '报价单'} · ${quote.status === 'issued' ? '已生成' : '草稿'}</small></span><button type="button" class="secondary" data-quote-edit="${esc(quote.id)}">${quote.status === 'draft' ? '编辑' : '查看'}</button>${quote.document_type !== 'pi' ? `<button type="button" class="secondary" data-quote-to-pi="${esc(quote.id)}">基于此报价生成 PI</button>` : ''}<button type="button" class="secondary" data-quote-download="${esc(quote.id)}">PDF</button></div>`).join('')}</div>` : '<p>还没有报价记录。</p>';
  } catch (error) { holder.textContent = error.message || '载入报价记录失败'; }
}

$('#quote-add-item').onclick = () => addQuoteItem();
$('#quote-form').addEventListener('input', updateQuoteTotal);
$('#quote-save-defaults').onclick = async () => {
  const payload = quotePayload();
  try {
    const response = await fetch('/api/quotes/defaults', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '保存失败');
    quoteDefaults = data.defaults; $('#quote-state').textContent = '公司抬头、常用条款和模板已保存为默认配置。';
  } catch (error) { $('#quote-state').textContent = error.message; }
};
$('#quote-save-customer').onclick = async () => {
  const form = $('#quote-form');
  try {
    const response = await fetch('/api/quotes/customers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: form.elements.namedItem('customer').value, address: form.elements.namedItem('customer_address').value }) });
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '保存客户失败');
    await loadQuoteCustomers(); $('#quote-customer-select').value = data.customer.id;
    $('#quote-state').textContent = `客户“${data.customer.name}”已保存，可下次直接选择。`;
  } catch (error) { $('#quote-state').textContent = error.message; }
};
async function importSelectedQuoteProducts() {
  if (!selectedQuoteProducts.size) { $('#quote-state').textContent = '请先在 AI 对话结果中勾选产品图片。'; return; }
  const button = $('#quote-import-chat'); button.disabled = true; $('#quote-state').textContent = '正在按图片目录匹配商品详情和报价资料…';
  try {
    const response = await fetch('/api/quotes/from-chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ document_type: $('#quote-form').elements.namedItem('document_type').value, language: $('#quote-form').elements.namedItem('language').value, currency: $('#quote-form').elements.namedItem('currency').value, model_id: $('#chat-model-select').value || 'local', selected_products: [...selectedQuoteProducts.keys()] }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '无法整理对话内容');
    const draft = data.draft;
    const current = quotePayload();
    const previousItems = new Map(current.items.filter((item) => item.image_file_id).map((item) => [String(item.image_file_id), item]));
    const importedItems = (draft.items || []).map((item) => {
      const previous = previousItems.get(String(item.image_file_id || ''));
      return previous ? { ...item, quantity: previous.quantity, unit_price: previous.unit_price, discount_percent: previous.discount_percent } : item;
    });
    const merged = { ...current, ...draft, document_type: current.document_type, language: current.language, template_name: current.template_name, company_name: current.company_name, company_address: current.company_address, company_phone: current.company_phone, company_email: current.company_email, company_contact: current.company_contact, currency: current.currency, quote_date: current.quote_date, exchange_rate: current.exchange_rate, exchange_date: current.exchange_date, origin_port: current.origin_port, destination_port: current.destination_port, insurance_term: current.insurance_term, incoterm: current.incoterm, payment_terms: current.payment_terms, payment_note: current.payment_note, shipping_term: current.shipping_term, tax_term: current.tax_term, tax_amount: current.tax_amount, tax_note: current.tax_note, shipping: current.shipping, other_fees: current.other_fees, items: importedItems.length ? importedItems : current.items };
    fillQuoteForm(merged);
    autoImportedSelection = [...selectedQuoteProducts.keys()].sort().join(',');
    $('#quote-state').textContent = `已匹配 ${draft.items?.length || 0} 个所选产品，每个勾选项单独一行。公司信息请填写；生成前可核对价格。`;
  } catch (error) { $('#quote-state').textContent = error.message || '整理失败'; }
  finally { button.disabled = false; }
}
$('#quote-import-chat').onclick = () => importSelectedQuoteProducts();
$('#quote-form').elements.namedItem('language').addEventListener('change', () => {
  const profile = availableCompanyProfiles.find((item) => item.id === $('#quote-company-profile').value);
  if (profile) applyCompanyProfile(profile);
  if (selectedQuoteProducts.size && !activeQuoteId && autoImportedSelection === [...selectedQuoteProducts.keys()].sort().join(',')) importSelectedQuoteProducts();
});
['company_name','company_address','company_phone','company_email'].forEach((name) => $('#quote-form').elements.namedItem(name).addEventListener('input', () => { $('#quote-company-profile').value = ''; }));
$('#quote-save').onclick = async () => {
  try { const data = await saveQuote(); $('#quote-state').textContent = `草稿已保存：${data.quote_no}`; toast('报价草稿已保存'); }
  catch (error) { $('#quote-state').textContent = error.message; }
};
$('#quote-pdf').onclick = async () => {
  const button = $('#quote-pdf'); button.disabled = true;
  try {
    const data = await saveQuote();
    const response = await fetch(`/api/quotes/${data.id}/pdf`, { method: 'POST' });
    if (!response.ok) { const error = await response.json(); throw new Error(error.detail || 'PDF 生成失败'); }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = `${data.quote_no}.pdf`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    activeQuoteStatus = 'issued';
    $('#quote-save').disabled = true;
    $('#quote-state').textContent = `已生成并下载 ${data.quote_no}.pdf。正式报价已锁定；如需修改请新建报价。`;
    await loadQuotes();
  } catch (error) { $('#quote-state').textContent = error.message || 'PDF 生成失败'; }
  finally { button.disabled = false; }
};
$('#quote-new').onclick = () => { autoImportedSelection = ''; fillQuoteForm({}); };
$('#quote-list').onclick = async (event) => {
  const edit = event.target.closest('[data-quote-edit]');
  const toPi = event.target.closest('[data-quote-to-pi]');
  const download = event.target.closest('[data-quote-download]');
  if (toPi) {
    try {
      const response = await fetch(`/api/quotes/${toPi.dataset.quoteToPi}`); const source = await response.json();
      if (!response.ok) throw new Error(source.detail || '无法读取报价单');
      const pi = { ...source.data, document_type: 'pi', quote_date: new Date().toISOString().slice(0, 10), notes: [source.data.notes, source.data.language === 'en' ? `Based on quotation ${source.quote_no}.` : `依据报价单 ${source.quote_no} 生成。`].filter(Boolean).join('\n') };
      fillQuoteForm(pi);
      pendingDocumentType = 'pi'; autoImportedSelection = '';
      $('#quote-form-title').textContent = '基于报价单新建 PI';
      $('#quote-state').textContent = `已从 ${source.quote_no} 带入产品、客户、价格和贸易条款；请核对后生成 PI。`;
      $('#quote-dialog').showModal();
      await loadQuoteTemplates();
      await Promise.all([loadQuoteCustomers(), loadCompanyProfiles()]);
      showQuoteStage(1);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (error) { toast(error.message || '无法从报价单生成 PI'); }
    return;
  }
  if (edit) {
    try {
      const response = await fetch(`/api/quotes/${edit.dataset.quoteEdit}`); const data = await response.json();
      if (!response.ok) throw new Error(data.detail);
      fillQuoteForm(data.data, data.id, data.quote_no);
      activeQuoteStatus = data.status;
      if (data.status === 'issued') { $('#quote-save').disabled = true; $('#quote-pdf').disabled = true; $('#quote-state').textContent = '已生成报价为只读记录。'; }
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (error) { toast(error.message || '无法打开报价'); }
  }
  if (download) {
    const response = await fetch(`/api/quotes/${download.dataset.quoteDownload}/pdf`);
    if (!response.ok) { const error = await response.json(); toast(error.detail || 'PDF 下载失败'); return; }
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = url; link.download = 'quotation.pdf'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 5000);
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
loadQuoteTemplates().then(() => fillQuoteForm({}));
loadQuotes();
