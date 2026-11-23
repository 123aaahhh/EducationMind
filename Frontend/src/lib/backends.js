import { pollEvaluation } from './evaluationJobs.js'
const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'Python',
    baseUrl: runtimeConfig().pythonApiUrl || import.meta.env.VITE_PYTHON_API_URL || '/api/python',
    port: '8000'
  },
  java: {
    id: 'java',
    label: 'Java',
    baseUrl: runtimeConfig().javaApiUrl || import.meta.env.VITE_JAVA_API_URL || '/api/java',
    port: '8080'
  }
}

export function createInitialSettings() {
  const saved = readSettings()
  return {
    pythonToken: '',
    backend: saved.backend || import.meta.env.VITE_DEFAULT_BACKEND || 'python',
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl,
      java: saved.endpoints?.java || DEFAULT_BACKENDS.java.baseUrl
    }
  }
}

export function saveSettings(settings) {
  localStorage.setItem('echomind.frontend.settings', JSON.stringify({ ...settings, pythonToken: undefined }))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS[type] || DEFAULT_BACKENDS.java
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints[type] || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestFor(type, settings, '/health')
}

export async function requestMonitor(type, settings) {
  return requestFor(type, settings, '/monitor')
}

export async function requestSkills(type, settings) {
  return requestFor(type, settings, '/skills')
}

export async function reloadSkills(type, settings) {
  return requestFor(type, settings, '/skills/reload', { method: 'POST' })
}

export async function requestKnowledgeStats(type, settings) {
  return requestFor(type, settings, '/knowledge/stats')
}

export async function runEvaluation(type, settings, body = null, options = {}) {
  const taskKey = `echomind.eval:${type}:${backendMeta(type, settings).baseUrl}:${settings.userId}`
  const savedJob = type === 'python' ? sessionStorage.getItem(taskKey) : null
  const initial = savedJob ? {job_id: savedJob, status: 'queued', completed: 0, total: 0} : await requestFor(type, settings, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  })
  if (!initial.job_id) return initial // Java 后端仍兼容同步报告。
  sessionStorage.setItem(taskKey, initial.job_id)
  try {
    const result = await pollEvaluation(initial, jobId => requestFor(type, settings,
      `/eval/jobs/${encodeURIComponent(jobId)}`), options)
    if (result) sessionStorage.removeItem(taskKey)
    return result
  } catch (error) {
    if (error.terminal || error.status === 404) sessionStorage.removeItem(taskKey)
    throw error
  }
}

export async function requestSearch(type, settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestFor(type, settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const attemptKey = JSON.stringify([type, meta.baseUrl, settings.userId, settings.conversationId, message])
  if (type === 'python') {
    if (!chatAttempts.has(attemptKey)) {
      if (chatAttempts.size > 100) chatAttempts.clear()
      chatAttempts.set(attemptKey, crypto.randomUUID())
    }
    payload.client_request_id = chatAttempts.get(attemptKey)
  }
  const raw = await requestFor(type, settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  chatAttempts.delete(attemptKey)
  return normalizeChatResponse(type, raw)
}

/**
 * SSE 流式对话：stage（阶段进度）→ delta（文本分片）→ result（完整元数据）。
 * 返回 Promise，中途可通过 AbortSignal 取消。
 */
export async function streamChat(type, settings, message, handlers = {}) {
  const { onStage, onDelta, signal } = handlers
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  if (type === 'python') payload.client_request_id = crypto.randomUUID()
  const headers = new Headers({ 'Content-Type': 'application/json', Accept: 'text/event-stream' })
  if (type === 'python' && settings.pythonToken) headers.set('Authorization', `Bearer ${settings.pythonToken}`)

  const response = await fetch(`${meta.baseUrl}/chat/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal
  })
  if (!response.ok) {
    const text = await response.text()
    let detail = text
    try {
      detail = JSON.parse(text).detail || detail
    } catch {
      // 保留原始文本
    }
    const error = new Error(`${response.status} ${detail}`)
    error.status = response.status
    throw error
  }
  if (!response.body) throw new Error('当前浏览器不支持流式响应')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result = null

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const rawEvent = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
      const event = parseSseEvent(rawEvent)
      if (!event) continue
      if (event.event === 'stage') {
        onStage?.(event.data)
      } else if (event.event === 'delta') {
        onDelta?.(event.data?.text || '')
      } else if (event.event === 'result') {
        result = normalizeChatResponse(type, event.data || {})
      } else if (event.event === 'error') {
        await reader.cancel().catch(() => {})
        const error = new Error(event.data?.message || '流式对话失败')
        error.status = event.data?.status || 500
        throw error
      }
    }
  }
  if (!result) throw new Error('流式连接意外结束，请重试')
  return result
}

function parseSseEvent(raw) {
  let event = 'message'
  const data = []
  for (const line of String(raw).split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).trim())
  }
  if (!data.length) return null
  const joined = data.join('\n')
  try {
    return { event, data: JSON.parse(joined) }
  } catch {
    return { event, data: joined }
  }
}

export async function listConversations(type, settings, { query = '', limit = 50 } = {}) {
  const params = new URLSearchParams({
    user_id: settings.userId || 'anonymous',
    limit: String(limit)
  })
  if (query) params.set('q', query)
  const raw = await requestFor(type, settings, `/conversations?${params}`)
  return raw?.items || []
}

export async function getConversation(type, settings, convId) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/conversations/${encodeURIComponent(convId)}?${params}`)
}

export async function renameConversation(type, settings, convId, title) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/conversations/${encodeURIComponent(convId)}?${params}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title })
  })
}

export async function removeConversation(type, settings, convId) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/conversations/${encodeURIComponent(convId)}?${params}`, {
    method: 'DELETE'
  })
}

export async function uploadHomeworkImage(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestFor(type, settings, '/ocr', { method: 'POST', body: form })
}

export function resolveAssetUrl(type, settings, path) {
  if (!path) return ''
  if (/^https?:\/\//i.test(path)) return path
  return `${backendMeta(type, settings).baseUrl}${path}`
}

export async function requestHint(type, settings, question, topic = '') {
  return requestFor(type, settings, '/learning/hint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, topic })
  })
}

export async function requestSteps(type, settings, question, topic = '') {
  return requestFor(type, settings, '/learning/steps', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, topic })
  })
}

export async function requestPractice(type, settings, { topic = '', question = '' } = {}) {
  return requestFor(type, settings, '/learning/practice', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, question })
  })
}

export async function addMistake(type, settings, payload) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/learning/mistakes?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

export async function requestMistakes(type, settings, limit = 50) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous', limit: String(limit) })
  const raw = await requestFor(type, settings, `/learning/mistakes?${params}`)
  return raw?.items || []
}

export async function removeMistake(type, settings, mistakeId) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/learning/mistakes/${encodeURIComponent(mistakeId)}?${params}`, {
    method: 'DELETE'
  })
}

export async function recordAttempt(type, settings, topic, correct) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/learning/attempt?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic, correct })
  })
}

export async function requestMastery(type, settings) {
  const params = new URLSearchParams({ user_id: settings.userId || 'anonymous' })
  return requestFor(type, settings, `/learning/mastery?${params}`)
}

export async function requestToolTrace(type, settings, requestId) {
  if (!requestId) return null
  const raw = await requestFor(type, settings, `/trace/tool/${encodeURIComponent(requestId)}`)
  return normalizeToolTraceResponse(raw)
}

export async function addKnowledge(type, settings, documents) {
  return requestFor(type, settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestFor(type, settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    verified: raw.verified,
    grounded: raw.grounded,
    raw
  }
}

function normalizeToolTraceResponse(raw) {
  const trace = raw?.trace || {}
  return {
    requestId: raw?.request_id || raw?.requestId || '',
    found: Boolean(raw?.found),
    trace: {
      ...trace,
      toolsUsed: trace.tools_used || trace.toolsUsed || [],
      toolCalls: trace.tool_calls || trace.toolCalls || []
    },
    raw
  }
}

const DEFAULT_CHAT_TIMEOUT_MS = 105000
const DEFAULT_REQUEST_TIMEOUT_MS = 20000

function requestTimeoutMs(path) {
  if (path === '/chat') {
    const configured = Number(runtimeConfig().chatTimeoutMs)
    return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_CHAT_TIMEOUT_MS
  }
  // 作业识别与学习辅导要调用视觉/大模型，给足服务端 30 秒预算的余量。
  if (path.startsWith('/ocr') || path.startsWith('/learning/')) return 45000
  return DEFAULT_REQUEST_TIMEOUT_MS
}

const chatAttempts = new Map()
async function requestFor(type, settings, path, options = {}) {
  const headers = new Headers(options.headers || {})
  if (type === 'python' && settings.pythonToken) headers.set('Authorization', `Bearer ${settings.pythonToken}`)
  return requestJson(backendMeta(type, settings).baseUrl, path, { ...options, headers,
    signal: options.signal || AbortSignal.timeout(requestTimeoutMs(path)) })
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    const error = new Error(`${response.status} ${response.statusText}: ${detail}`)
    error.status = response.status
    throw error
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('echomind.frontend.settings') || '{}')
  } catch {
    return {}
  }
}

function runtimeConfig() {
  if (typeof window === 'undefined') return {}
  return window.__ECHOMIND_CONFIG__ || {}
}
