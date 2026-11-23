<template>
  <div class="app-shell" :class="`view-${activeView}`">
    <aside class="navigation-rail" aria-label="主导航">
      <a class="brand" href="#" @click.prevent="activeView = 'chat'" aria-label="EducationAgent 首页">
        <span class="brand-mark">E</span>
        <span class="brand-name">EducationAgent</span>
      </a>
      <button class="new-chat-button" @click="activeView = 'chat'; clearConversation()">
        <AppIcon name="plus" />
        <span>新对话</span>
      </button>
      <span class="nav-caption">工作空间</span>
      <nav class="view-nav">
        <button
          v-for="view in views"
          :key="view.id"
          :aria-label="view.label"
          :class="{ active: activeView === view.id }"
          :aria-current="activeView === view.id ? 'page' : undefined"
          @click="activeView = view.id"
        >
          <AppIcon :name="view.icon" />
          <span>{{ view.label }}</span>
          <AppIcon class="nav-chevron" name="chevron" />
        </button>
      </nav>
      <div class="rail-bottom">
        <a class="docs-link" aria-label="API 文档" :href="docsUrl" target="_blank" rel="noreferrer">
          <AppIcon name="document" />
          <span>API 文档</span>
          <AppIcon name="northeast" />
        </a>
        <button class="user-button" aria-label="当前用户与会话配置" @click="openConnection">
          <span class="avatar">{{ userInitial }}</span>
          <span>
            <strong>{{ settings.userId || 'anonymous' }}</strong>
            <small>个人工作空间</small>
          </span>
          <AppIcon name="sliders" />
        </button>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="page-breadcrumb">
          <span>工作空间</span>
          <AppIcon name="chevron" />
          <strong>{{ currentView.label }}</strong>
        </div>
        <a class="mobile-brand" href="#" @click.prevent="activeView = 'chat'" aria-label="EducationAgent 首页">
          <span class="brand-mark">E</span>
          <span>EducationAgent</span>
        </a>
        <div class="topbar-tools">
          <span class="connection-status" :title="statusText || healthLabel">
            <i :class="healthOk ? 'online' : 'offline'"></i>
            {{ healthOk ? '已连接' : healthLabel === '未检查' ? '连接中' : '未连接' }}
          </span>
          <div class="backend-tabs" aria-label="选择后端">
            <button
              v-for="backend in ['java', 'python']"
              :key="backend"
              :class="{ active: settings.backend === backend }"
              :aria-pressed="settings.backend === backend"
              @click="switchBackend(backend)"
            >
              {{ backend === 'java' ? 'Java' : 'Python' }}
            </button>
          </div>
          <button class="icon-button settings-button" aria-label="连接配置" title="连接配置" @click="openConnection">
            <AppIcon name="sliders" />
          </button>
        </div>
      </header>
      <div v-if="toast" class="toast" role="status">
        <AppIcon name="chat" />
        {{ toast }}
      </div>
      <section v-if="activeView === 'chat'" class="page-chat">
        <div class="chat-toolbar">
          <span class="conversation-label">
            {{ messages.length ? '当前对话' : '新对话' }}
            <span v-if="messages.length" class="conversation-count">{{ messages.length }} 条消息</span>
          </span>
          <div class="chat-toolbar-actions">
            <button v-if="supportsEducationTools" class="trace-toggle" @click="openHistory">
              <AppIcon name="book" />
              <span>历史对话</span>
            </button>
            <button class="icon-button" @click="clearConversation" aria-label="新对话" title="新对话">
              <AppIcon name="compose" />
            </button>
            <button class="trace-toggle" @click="inspectorDialog.showModal()">
              <AppIcon name="panel" />
              <span>运行详情</span>
            </button>
          </div>
        </div>
        <div class="conversation-layout" :class="{ 'has-conversation': messages.length }">
          <section class="conversation" :class="{ 'is-empty': !messages.length }" aria-label="在线教育辅导对话">
            <div class="messages" ref="messageList" role="log" aria-label="对话消息" aria-live="polite" :aria-busy="busy && pendingAction === 'chat'">
              <div v-if="!messages.length" class="welcome">
                <div class="welcome-mark" aria-hidden="true">📚</div>
                <h1>在线教育辅导，有什么可以帮你？</h1>
                <p>知识点讲解、作业辅导、学习计划、考试咨询——专业 Agent 自动接手。</p>
              </div>
              <template v-else>
                <article v-for="item in messages" :key="item.id" :class="['message', item.role, { 'message-error': item.error }]">
                  <span v-if="item.role === 'assistant'" class="message-avatar">E</span>
                  <div class="message-content">
                    <div class="message-meta">
                      <strong>{{ item.role === 'user' ? '你' : 'EducationAgent' }}</strong>
                      <span v-if="item.error" class="error-label">请求失败</span>
                      <small v-else-if="item.role === 'assistant'">{{ agentDisplayName(item._agentType) }}</small>
                    </div>
                    <div v-if="item.streaming" class="stage-progress" role="status" aria-live="polite">
                      <span
                        v-for="stage in stageFlow"
                        :key="stage.id"
                        :class="['stage-chip', stageState(item, stage.id)]"
                      >
                        <AppIcon
                          :name="stageState(item, stage.id) === 'done' ? 'check' : 'refresh'"
                          :class="{ spinning: stageState(item, stage.id) === 'active' }"
                        />
                        {{ stage.label }}
                      </span>
                    </div>
                    <img
                      v-if="item.imageUrl"
                      :src="item.imageUrl"
                      class="message-image"
                      alt="作业原图，供人工核验"
                    />
                    <div v-if="item.practice" class="practice-card">
                      <span class="practice-label">同类练习 · {{ item.practice.topic }}</span>
                      <MarkdownView :content="item.practice.question" />
                      <button
                        v-if="!item.practice.revealed"
                        class="text-button"
                        @click="item.practice.revealed = true"
                      >
                        查看答案与解析
                      </button>
                      <template v-else>
                        <MarkdownView :content="item.practice.answer" />
                        <MarkdownView v-if="item.practice.explanation" :content="item.practice.explanation" />
                        <div class="practice-actions">
                          <button :disabled="item.practice.graded" @click="gradePractice(item, true)">我答对了</button>
                          <button :disabled="item.practice.graded" class="danger-button" @click="gradePractice(item, false)">我没答对</button>
                        </div>
                      </template>
                    </div>
                    <MarkdownView v-else-if="item.role === 'assistant' && item.content" :content="item.content" />
                    <p v-else-if="item.role === 'user'" class="user-text">{{ item.content }}</p>
                    <span v-if="item.meta && !item.error" class="agent-tag">
                      <AppIcon name="route" />
                      {{ item.meta }}
                    </span>
                    <details v-if="item.trace?.toolCalls?.length || item.trace?.toolsUsed?.length" class="message-trace">
                      <summary>
                        <AppIcon name="chevron" />
                        <span>工具调用</span>
                        <span class="count-tag">{{ item.trace.toolCalls?.length || 0 }}</span>
                      </summary>
                      <div v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" class="trace-call-item">
                        <div class="trace-call-meta">
                          <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                          <span :class="call.success ? 'success' : 'danger'">{{ call.success ? '成功' : '失败' }}</span>
                        </div>
                        <pre>{{ formatJson(call.input || {}) }}</pre>
                      </div>
                      <p v-if="!item.trace.toolCalls?.length" class="muted">已调用：{{ item.trace.toolsUsed.join(' · ') }}</p>
                    </details>
                    <button
                      v-if="item.role === 'assistant' && !item.error"
                      class="icon-button copy-button"
                      @click="copyMessage(item.content)"
                      aria-label="复制回复"
                      title="复制回复"
                    >
                      <AppIcon name="copy" />
                    </button>
                    <div
                      v-if="item.role === 'assistant' && !item.error && !item.streaming && isLatestAssistant(item) && supportsEducationTools"
                      class="learning-actions"
                    >
                      <button :disabled="busy" @click="requestHintAction(item)">
                        <AppIcon name="chat" />
                        提示
                      </button>
                      <button :disabled="busy" @click="requestStepsAction(item)">
                        <AppIcon name="document" />
                        分步讲解
                      </button>
                      <button :disabled="busy" @click="requestPracticeAction(item)">
                        <AppIcon name="play" />
                        同类练习
                      </button>
                      <button :disabled="busy" @click="saveMistake(item)">
                        <AppIcon name="book" />
                        记入错题
                      </button>
                    </div>
                  </div>
                </article>
                <div v-if="busy && pendingAction === 'chat' && !streaming" class="thinking" role="status">
                  <span class="message-avatar">E</span>
                  <span class="thinking-dots">
                    <i></i>
                    <i></i>
                    <i></i>
                  </span>
                  <span>正在处理问题</span>
                </div>
              </template>
            </div>
            <div class="composer-area">
              <form class="composer" @submit.prevent="performAction('chat', sendMessage)">
                <div v-if="attachment" class="attachment-preview">
                  <img :src="attachment.imageUrl" alt="作业原图" />
                  <div class="attachment-body">
                    <strong>{{ attachment.original_name || '作业照片' }}</strong>
                    <textarea v-model="attachment.text" rows="3" placeholder="识别结果，可手动修正后发送"></textarea>
                    <small v-if="attachment.warning" class="warn-text">{{ attachment.warning }}</small>
                    <small v-else>原图会随消息保留，便于老师或家长核验。</small>
                  </div>
                  <button type="button" class="icon-button" aria-label="移除作业图片" @click="attachment = null">
                    <AppIcon name="close" />
                  </button>
                </div>
                <textarea
                  ref="composerInput"
                  v-model="draft"
                  rows="2"
                  aria-label="描述你的学习问题"
                  placeholder="比如：什么是牛顿第二定律 / 这道数学题怎么做 / 帮我制定学习计划…"
                  @keydown.meta.enter.prevent="performAction('chat', sendMessage)"
                  @keydown.ctrl.enter.prevent="performAction('chat', sendMessage)"
                >
                </textarea>
                <div class="composer-bottom">
                  <div class="composer-tools">
                    <span class="routing-label">
                      <AppIcon name="route" />
                      Agent 自动路由
                    </span>
                    <label
                      v-if="supportsEducationTools"
                      class="icon-button upload-trigger"
                      title="上传作业照片，自动识别文字与公式"
                      :class="{ disabled: busy || streaming }"
                    >
                      <AppIcon name="upload" />
                      <input type="file" accept="image/*" hidden :disabled="busy || streaming" @change="handleOcrUpload" />
                    </label>
                  </div>
                  <div class="send-controls">
                    <span v-if="busy && !streaming" class="keyboard-hint">处理中，请稍候</span>
                    <span v-else class="keyboard-hint">⌘ / Ctrl + Enter</span>
                    <button
                      v-if="streaming"
                      class="send-button stop-button"
                      type="button"
                      aria-label="停止生成"
                      title="停止生成"
                      @click="cancelStreaming"
                    >
                      <AppIcon name="close" />
                    </button>
                    <button
                      v-else
                      class="send-button"
                      type="submit"
                      :disabled="busy || (!draft.trim() && !attachment?.text?.trim())"
                      :aria-label="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                      :title="busy && pendingAction === 'chat' ? '处理中' : '发送'"
                    >
                      <AppIcon :name="busy && pendingAction === 'chat' ? 'refresh' : 'arrow'" :class="{ spinning: busy && pendingAction === 'chat' }" />
                    </button>
                  </div>
                </div>
              </form>
              <div v-if="!messages.length" class="prompt-grid">
                <button v-for="prompt in prompts" :key="prompt.label" @click="usePrompt(prompt.text)">
                  <AppIcon :name="prompt.icon" />
                  <span>
                    <strong>{{ prompt.label }}</strong>
                    <small>{{ prompt.desc }}</small>
                  </span>
                  <AppIcon class="prompt-arrow" name="northeast" />
                </button>
              </div>
              <p class="composer-note">AI 生成的讲解与学习建议仅供参考，重要学习决策请与老师或家长确认。</p>
            </div>
          </section>
          <aside v-if="messages.length" class="inspector-desktop" aria-label="请求追踪与运行状态">
            <div class="inspector-heading">
              <AppIcon name="panel" />
              <h2>运行详情</h2>
            </div>
            <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
          </aside>
        </div>
      </section>
      <section v-else-if="activeView === 'knowledge'" class="page page-knowledge">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">KNOWLEDGE OPERATIONS</span>
            <h1>知识库</h1>
            <p>管理学习资料，为每次回答提供依据。</p>
          </div>
          <div class="count-display">
            <strong>{{ knowledgeCount }}</strong>
            <span>知识片段</span>
          </div>
        </div>
        <div class="knowledge-layout">
          <section class="workspace-card search-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">RETRIEVAL</span>
                <h2>检索知识</h2>
              </div>
              <AppIcon name="search" />
            </div>
            <form class="search-line" @submit.prevent="performAction('search', searchKnowledge)">
              <input v-model="searchQuery" aria-label="检索问题" placeholder="例如：牛顿第二定律讲解" />
              <button :disabled="busy || !searchQuery.trim()">
                <AppIcon name="search" />
                {{ busy && pendingAction === 'search' ? '检索中' : '搜索' }}
              </button>
            </form>
            <div v-if="searchResults.length" class="result-list">
              <article v-for="(item, index) in searchResults" :key="item.id || index" class="result-item">
                <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
                <div>
                  <div class="result-title">
                    <strong>{{ item.title || '未命名文档' }}</strong>
                    <small>相关度 {{ item.score ?? '—' }}</small>
                  </div>
                  <p>{{ item.content }}</p>
                </div>
              </article>
            </div>
            <div v-else class="workspace-empty">
              <AppIcon name="book" />
              <h3>{{ searched ? '暂未找到相关知识' : '搜索你的知识库' }}</h3>
              <p>{{ searched ? '尝试换一个问题，或添加更多学习资料。' : '输入学习问题，查看最相关的知识片段。' }}</p>
            </div>
          </section>
          <section class="workspace-card import-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">INGESTION</span>
                <h2>添加知识</h2>
              </div>
              <AppIcon name="document" />
            </div>
            <form @submit.prevent="performAction('import', submitKnowledge)">
              <label>
                <span>文档标题</span>
                <input v-model="docTitle" required placeholder="例如：高中数学函数知识点总结" />
              </label>
              <label>
                <span>知识内容</span>
                <textarea v-model="docContent" required rows="7" placeholder="输入教材章节、知识点讲解、题型分析、考试大纲等内容"></textarea>
              </label>
              <div class="side-actions">
                <button :disabled="busy || !docTitle.trim() || !docContent.trim()">
                  <AppIcon name="arrow" />
                  {{ busy && pendingAction === 'import' ? '添加中' : '添加文档' }}
                </button>
                <label class="upload-button" :class="{ disabled: busy }">
                  <AppIcon name="upload" />
                  上传文件
                  <input :disabled="busy" type="file" accept=".txt,.md,.json" @change="performAction('upload', handleUpload, $event)" />
                </label>
              </div>
              <p class="form-hint">支持 TXT、Markdown、JSON，最大 10 MB。</p>
            </form>
          </section>
        </div>
        <section class="workspace-card skills-workspace">
          <div class="card-heading">
            <div>
              <span class="kicker">LOADED SKILLS</span>
              <h2>已加载能力</h2>
            </div>
            <button class="text-button" :disabled="busy" @click="performAction('skills', reloadSkillSet)">
              <AppIcon name="refresh" />
              重新加载
            </button>
          </div>
          <div class="skill-table">
            <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
              <span class="skill-dot"></span>
              <strong>{{ skill.name }}</strong>
              <span>{{ skill.description || '教学规范能力' }}</span>
              <small>{{ skill.content_chars || 0 }} chars</small>
            </div>
            <p v-if="!skillsData.skills?.length" class="skills-empty">暂无已加载能力，连接服务后刷新查看。</p>
          </div>
        </section>
      </section>
      <section v-else-if="activeView === 'learning'" class="page page-learning">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">LEARNING LOOP</span>
            <h1>学习闭环</h1>
            <p>提示 → 分步讲解 → 同类练习 → 错题记录 → 掌握度，形成可持续的学习轨迹。</p>
          </div>
          <div class="count-display">
            <strong>{{ masteryPercent(learning.accuracy) }}</strong>
            <span>练习正确率</span>
          </div>
        </div>
        <div class="learning-layout">
          <section class="workspace-card mastery-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">MASTERY</span>
                <h2>知识点掌握度</h2>
              </div>
              <button class="text-button" :disabled="busy" @click="loadLearning">
                <AppIcon name="refresh" />
                刷新
              </button>
            </div>
            <div v-if="learning.mastery.length" class="mastery-list">
              <article v-for="item in learning.mastery" :key="item.topic" class="mastery-item">
                <div class="mastery-head">
                  <strong>{{ item.topic }}</strong>
                  <span>{{ masteryPercent(item.mastery) }}</span>
                </div>
                <div class="mastery-bar"><i :style="{ width: masteryPercent(item.mastery) }"></i></div>
                <small>{{ item.correct }} / {{ item.total }} 次答对</small>
              </article>
            </div>
            <div v-else class="workspace-empty">
              <AppIcon name="chart" />
              <h3>还没有练习记录</h3>
              <p>在对话中点击「同类练习」，完成后即可看到掌握度。</p>
              <button class="text-button" @click="scrollToComposer">去对话练习</button>
            </div>
          </section>
          <section class="workspace-card mistakes-workspace">
            <div class="card-heading">
              <div>
                <span class="kicker">MISTAKES</span>
                <h2>错题记录</h2>
              </div>
              <AppIcon name="book" />
            </div>
            <div v-if="learning.recent_mistakes.length" class="mistake-list">
              <article v-for="item in learning.recent_mistakes" :key="item.id" class="mistake-item">
                <div class="mistake-head">
                  <strong>{{ item.topic }}</strong>
                  <small>{{ formatTime(item.created_at) }}</small>
                  <button class="icon-button" aria-label="移除错题" @click="deleteMistakeItem(item)">
                    <AppIcon name="trash" />
                  </button>
                </div>
                <MarkdownView :content="item.question" />
                <details v-if="item.answer">
                  <summary>查看当时的讲解</summary>
                  <MarkdownView :content="item.answer" />
                </details>
              </article>
            </div>
            <div v-else class="workspace-empty">
              <AppIcon name="book" />
              <h3>错题本还是空的</h3>
              <p>在对话回复下点击「记入错题」，复习时可以快速找回。</p>
            </div>
          </section>
        </div>
        <section class="workspace-card">
          <div class="card-heading">
            <div>
              <span class="kicker">FOCUS</span>
              <h2>练习主题</h2>
            </div>
          </div>
          <form class="search-line" @submit.prevent="scrollToComposer">
            <input v-model="learningDraft.topic" aria-label="练习知识点" placeholder="例如：一元二次方程、牛顿第二定律（留空则由题目自动判断）" />
            <button type="submit">
              <AppIcon name="route" />
              去对话练习
            </button>
          </form>
        </section>
      </section>
      <section v-else class="page page-evaluation">
        <div class="page-heading">
          <div class="heading-copy">
            <span class="kicker">EVALUATION LAB</span>
            <h1>评测中心</h1>
            <p>检查意图识别、回复质量与回归结果。</p>
          </div>
          <button @click="performAction('evaluation', runEvaluation)" :disabled="busy">
            <AppIcon :name="busy && pendingAction === 'evaluation' ? 'refresh' : 'play'" :class="{ spinning: busy && pendingAction === 'evaluation' }" />
            {{ busy && pendingAction === 'evaluation' ? '评测运行中' : '运行评测' }}
          </button>
        </div>
        <div v-if="evalData" class="evaluation-content">
          <div class="evaluation-summary">
            <div class="score-hero">
              <span>PASS RATE · 通过率</span>
              <strong :class="{ 'no-score': evalData.valid_total === 0 }">{{ evalData.valid_total === 0 ? '无有效评分' : formatPercent(evalData.pass_rate) }}</strong>
              <small>{{ evalData.passed }} / {{ evalData.valid_total ?? evalData.total }} 有效用例通过</small>
            </div>
            <div>
              <span>通过用例</span>
              <strong>{{ evalData.passed }}</strong>
            </div>
            <div>
              <span>总数 / 评分失败</span>
              <strong>
                {{ evalData.total }}
                <small> / {{ evalData.evaluation_errors ?? 0 }}</small>
              </strong>
            </div>
            <div>
              <span>回归问题</span>
              <strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong>
            </div>
          </div>
          <div class="evaluation-layout">
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">QUALITY SCORES</span>
                  <h2>平均评分</h2>
                </div>
              </div>
              <p v-if="!Object.keys(evalData.avg_scores || {}).length" class="muted">没有有效评分，请查看失败原因。</p>
              <div class="score-list">
                <div v-for="(value, key) in evalData.avg_scores" :key="key">
                  <span>{{ key }}</span>
                  <i>
                    <b :style="{ width: `${Math.max(0, Math.min(Number(value) * 100, 100))}%` }"></b>
                  </i>
                  <strong>{{ Number(value).toFixed(2) }}</strong>
                </div>
              </div>
            </section>
            <section class="workspace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">RECOMMENDATIONS</span>
                  <h2>优化建议</h2>
                </div>
              </div>
              <p v-if="evalData.regression_note" class="muted">{{ evalData.regression_note }}</p>
              <div v-if="evalData.recommendations?.length" class="recommendations">
                <p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p>
              </div>
              <p v-else class="muted">本次评测没有返回额外建议。</p>
              <div v-for="(result, index) in evalData.regressions || []" :key="index" class="evaluation-warning">
                <strong class="danger">回归问题</strong>
                <p>{{ typeof result === 'string' ? result : formatJson(result) }}</p>
              </div>
              <div v-for="result in (evalData.results || []).filter(item => item.metadata?.judge_failed)" :key="result.test_id" class="evaluation-warning">
                <strong class="danger">{{ result.test_id }}：评分失败</strong>
                <p>{{ result.metadata.judge_error || '模型未返回有效评分' }}</p>
                <small>{{ result.metadata.judge_attempts ?? '—' }} 次尝试</small>
              </div>
            </section>
          </div>
        </div>
        <div v-else class="evaluation-empty">
          <span class="empty-emblem">
            <AppIcon name="chart" />
          </span>
          <h2>{{ busy && pendingAction === 'evaluation' ? '正在检验每一份答案。' : '了解 Agent 的表现' }}</h2>
          <p>{{ busy && pendingAction === 'evaluation' ? '评测需要一些时间，结果返回后会显示在这里。' : '运行一次评测，了解 Agent 的表现与改进空间。' }}</p>
          <div class="evaluation-dimensions">
            <span>意图识别</span>
            <span>回复质量</span>
            <span>回归检测</span>
          </div>
        </div>
      </section>
    </main>
    <dialog ref="inspectorDialog" class="inspector-dialog" @click="closeOnBackdrop" aria-labelledby="inspector-title">
      <div class="drawer-heading">
        <div>
          <AppIcon name="panel" />
          <h2 id="inspector-title">运行详情</h2>
        </div>
        <button class="icon-button" aria-label="关闭运行详情" @click="inspectorDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <RequestInspector :last-response="lastResponse" :last-trace="lastTrace" :busy="busy && pendingAction === 'chat'" :monitor-data="monitorData" @refresh="loadMonitor" />
    </dialog>
    <dialog ref="historyDialog" class="history-dialog" @click="closeOnBackdrop" aria-labelledby="history-title">
      <div class="dialog-heading">
        <div>
          <span class="kicker">CONVERSATIONS</span>
          <h2 id="history-title">历史对话</h2>
        </div>
        <button class="icon-button" aria-label="关闭历史对话" @click="historyDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <div class="history-search">
        <AppIcon name="search" />
        <input v-model="historyQuery" placeholder="搜索标题或摘要" @input="scheduleHistorySearch" />
      </div>
      <div class="history-list">
        <article
          v-for="item in conversations"
          :key="item.conv_id"
          class="history-item"
          :class="{ active: item.conv_id === settings.conversationId }"
        >
          <div class="history-main" role="button" tabindex="0" @click="openConversation(item)" @keydown.enter.prevent="openConversation(item)">
            <input
              v-if="editingConvId === item.conv_id"
              v-model="editingTitle"
              class="rename-input"
              maxlength="60"
              @click.stop
              @keydown.enter.prevent="confirmRename(item)"
              @keydown.esc.prevent="editingConvId = ''"
            />
            <strong v-else>{{ item.title }}</strong>
            <small>{{ item.preview || '暂无摘要' }}</small>
            <span>{{ formatTime(item.updated_at) }} · {{ item.message_count }} 条消息</span>
          </div>
          <div class="history-actions">
            <button class="icon-button" aria-label="重命名会话" title="重命名" @click.stop="startRename(item)">
              <AppIcon name="compose" />
            </button>
            <button class="icon-button" aria-label="删除会话" title="删除" @click.stop="deleteConversationItem(item)">
              <AppIcon name="trash" />
            </button>
          </div>
        </article>
        <p v-if="!conversations.length" class="history-empty">
          {{ historyQuery ? '没有匹配的会话，换个关键词试试。' : '还没有历史对话，先问一个学习问题吧。' }}
        </p>
      </div>
      <p class="history-note">会话记录保留 24 小时；作业原图按 7 天保留期自动清理。</p>
    </dialog>
    <dialog ref="connectionDialog" class="connection-dialog" @click="closeOnBackdrop" aria-labelledby="connection-title">
      <div class="dialog-heading">
        <div>
          <span class="kicker">CONNECTION</span>
          <h2 id="connection-title">连接配置</h2>
        </div>
        <button class="icon-button" aria-label="关闭连接配置" @click="connectionDialog.close()">
          <AppIcon name="close" />
        </button>
      </div>
      <p class="dialog-description">配置 {{ currentBackend.label }} 服务与当前会话。</p>
      <form @submit.prevent="saveConnection">
        <label>
          <span>服务地址</span>
          <input v-model="connectionDraft.endpoint" required placeholder="/api/python 或 http://localhost:8000" />
          <small>使用代理路径或完整的 HTTP / HTTPS 地址。</small>
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="connectionDraft.userId" placeholder="u1001" />
        </label>
        <label>
          <span>会话 ID</span>
          <input v-model="connectionDraft.conversationId" placeholder="留空以自动生成" />
        </label>
        <label v-if="settings.backend === 'python'">
          <span>API 访问 Token（启用鉴权时填写，仅本页保存）</span>
          <input v-model="connectionDraft.pythonToken" type="password" autocomplete="off" placeholder="不是模型 API Key" />
        </label>
        <div class="dialog-health">
          <i :class="healthOk ? 'online' : 'offline'"></i>
          <span>{{ healthOk ? '服务已连接' : healthLabel }}</span>
          <button type="button" class="text-button" @click="checkHealth">
            检查当前连接
            <AppIcon name="refresh" />
          </button>
        </div>
        <details v-if="statusText" class="connection-detail">
          <summary>查看连接详情</summary>
          <pre>{{ statusText }}</pre>
        </details>
        <div class="dialog-actions">
          <button type="button" class="quiet-button" @click="connectionDialog.close()">取消</button>
          <button type="submit">
            保存配置
            <AppIcon name="check" />
          </button>
        </div>
      </form>
    </dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  addMistake,
  backendMeta,
  createInitialSettings,
  getConversation,
  listConversations,
  recordAttempt,
  reloadSkills,
  removeConversation,
  removeMistake,
  renameConversation,
  requestChat,
  requestHealth,
  requestHint,
  requestKnowledgeStats,
  requestMastery,
  requestMonitor,
  requestPractice,
  requestSearch,
  requestSteps,
  requestToolTrace,
  requestSkills,
  resolveAssetUrl,
  runEvaluation as requestEvaluation,
  saveSettings,
  streamChat,
  uploadHomeworkImage,
  uploadKnowledge
} from './lib/backends'

import AppIcon from './components/AppIcon.vue'
import MarkdownView from './components/MarkdownView.vue'
import RequestInspector from './components/RequestInspector.vue'

import { createRequestState } from './lib/requestState'

const settings = reactive(createInitialSettings())
const requestState = createRequestState(settings)
const activeView = ref('chat')
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('牛顿第二定律')
const searchResults = ref([])
const docTitle = ref('')
const docContent = ref('')
const messageList = ref(null)
const composerInput = ref(null)
const connectionDialog = ref(null)
const inspectorDialog = ref(null)
const historyDialog = ref(null)
const connectionDraft = reactive({ endpoint: '', userId: '', conversationId: '', pythonToken: '' })
const pendingAction = ref('')
const searched = ref(false)
const streaming = ref(false)
const streamAbort = ref(null)
const attachment = ref(null)
const conversations = ref([])
const historyQuery = ref('')
const editingConvId = ref('')
const editingTitle = ref('')
const learning = reactive({ mastery: [], recent_mistakes: [], attempts: 0, accuracy: 0, weakest_topic: '' })
const learningDraft = reactive({ topic: '' })
let historySearchTimer
const stageFlow = [
  { id: 'identify', label: '识别问题' },
  { id: 'retrieve', label: '检索资料' },
  { id: 'generate', label: '生成讲解' }
]
const views = [
  { id: 'chat', label: '对话', icon: 'chat' },
  { id: 'knowledge', label: '知识库', icon: 'book' },
  { id: 'learning', label: '学习闭环', icon: 'check' },
  { id: 'evaluation', label: '评测', icon: 'chart' }
]
const prompts = [
  { label: '什么是牛顿第二定律', text: '什么是牛顿第二定律，能用生活例子讲解吗', desc: '知识点讲解 · TutorAgent', icon: 'sliders' },
  { label: '这道数学题怎么做', text: '已知函数 f(x)=x²+2x-3，求顶点坐标', desc: '作业辅导 · TutorAgent', icon: 'route' },
  { label: '帮我制定学习计划', text: '高三理科生，想考985，帮我制定复习计划', desc: '学习规划 · StudyCoachAgent', icon: 'document' }
]

const AGENT_NAME_MAP = {
  TUTOR: '学科辅导 · TutorAgent',
  STUDY_COACH: '学习规划 · StudyCoachAgent',
  EXAM_ADVISOR: '考试顾问 · ExamAdvisorAgent',
  GENERAL: '学习助手 · GeneralAgent',
  ESCALATION: '人工支持 · EscalationAgent'
}

function agentDisplayName(type) {
  if (!type) return 'EducationAgent'
  return AGENT_NAME_MAP[type] || type.replace(/_/g, ' ')
}
const currentView = computed(() => views.find(view => view.id === activeView.value))
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const evalData = ref(null)
const toast = ref('')
let toastTimer
let messageSequence = 0

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const userInitial = computed(() => (settings.userId || 'U').slice(0, 1).toUpperCase())
const supportsEducationTools = computed(() => settings.backend === 'python')

watch(() => settings.conversationId, persist)
onMounted(async () => {
  await refreshConsole()
  await restoreConversation()
  await loadLearning()
})
onBeforeUnmount(() => {
  cancelStreaming()
  requestState.invalidate()
  clearTimeout(toastTimer)
  clearTimeout(historySearchTimer)
})

function persist() { saveSettings(settings) }

function performAction(name, action, ...args) {
  if (busy.value) return
  pendingAction.value = name
  return action(...args)
}

function openConnection() {
  connectionDraft.pythonToken = settings.pythonToken
  connectionDraft.endpoint = currentBackend.value.baseUrl
  connectionDraft.userId = settings.userId
  connectionDraft.conversationId = settings.conversationId
  connectionDialog.value.showModal()
}

function closeOnBackdrop(event) {
  const dialog = event.currentTarget
  if (event.target !== dialog) return
  const rect = dialog.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close()
}

function saveConnection() {
  const endpoint = connectionDraft.endpoint.trim().replace(/\/+$/, '')
  if (!endpoint || !(endpoint.startsWith('/') || /^https?:\/\//i.test(endpoint))) {
    showToast('请输入代理路径或 HTTP / HTTPS 服务地址')
    return
  }
  const changed = connectionDraft.pythonToken !== settings.pythonToken || endpoint !== currentBackend.value.baseUrl || connectionDraft.userId.trim() !== settings.userId || connectionDraft.conversationId.trim() !== settings.conversationId
  if (changed) {
    requestState.invalidate()
    busy.value = false
    messages.value = []
    lastResponse.value = null
    lastTrace.value = null
    searchResults.value = []
    searched.value = false
    evalData.value = null
    monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
    skillsData.value = { count: 0, skills: [], errors: [] }
    knowledgeCount.value = '—'
    healthOk.value = false
    healthLabel.value = '未检查'
    statusText.value = ''
  }
  const identityChanged = connectionDraft.userId.trim() !== settings.userId || endpoint !== currentBackend.value.baseUrl
  settings.endpoints[settings.backend] = endpoint
  settings.pythonToken = connectionDraft.pythonToken.trim()
  settings.userId = connectionDraft.userId.trim()
  settings.conversationId = identityChanged && connectionDraft.conversationId.trim() === settings.conversationId ? '' : connectionDraft.conversationId.trim()
  persist()
  connectionDialog.value.close()
  refreshConsole()
  showToast('连接配置已保存')
}

async function copyMessage(content) {
  try {
    await navigator.clipboard.writeText(content)
    showToast('回复已复制')
  } catch {
    showToast('复制失败，请手动选择回复文字')
  }
}

function switchBackend(type) {
  if (settings.backend === type) return
  searched.value = false
  requestState.invalidate()
  busy.value = false
  statusText.value = ''
  knowledgeCount.value = '-'
  monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  skillsData.value = { count: 0, skills: [], errors: [] }
  evalData.value = null
  settings.backend = type
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  messages.value = []
  searchResults.value = []
  lastResponse.value = null
  lastTrace.value = null
  refreshConsole()
}

async function refreshConsole() {
  await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills()])
}

async function checkHealth() {
  const request = requestState.begin('health')

  try {
    const data = await requestHealth(request.backend, request.settings)
    if (!request.isCurrent()) return
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    if (!request.isCurrent()) return
    // 健康检查只更新状态，保留用户选择的后端。
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function loadStats() {
  const request = requestState.begin('stats')
  try {
    const data = await requestKnowledgeStats(request.backend, request.settings)
    if (!request.isCurrent()) return
    knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch {
    if (request.isCurrent()) knowledgeCount.value = '-'
  }
}

async function loadMonitor() {
  const request = requestState.begin('monitor')
  try {
    const data = await requestMonitor(request.backend, request.settings)
    if (request.isCurrent()) monitorData.value = { ...data, loaded: true }
  } catch {
    if (request.isCurrent()) monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
  }
}

async function loadSkills() {
  const request = requestState.begin('skills')
  try {
    const data = await requestSkills(request.backend, request.settings)
    if (request.isCurrent()) skillsData.value = data
  } catch {
    if (request.isCurrent()) skillsData.value = { count: 0, skills: [], errors: [] }
  }
}

async function reloadSkillSet() {
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await reloadSkills(request.backend, request.settings)
    if (!request.isCurrent()) return
    skillsData.value = data
    showToast('Skills 已重新加载')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('Skills 加载失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

function scrollToBottom() {
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
}

function buildMeta(response) {
  return [
    response.intent,
    response.primaryAgent || response.agentType,
    response.knowledgeUsed ? 'RAG' : '',
    response.escalated ? '转人工' : ''
  ].filter(Boolean).join(' · ')
}

function advanceStage(message, stage) {
  const order = stageFlow.map(item => item.id)
  const index = order.indexOf(stage)
  if (index < 0) return
  order.forEach((id, i) => {
    message.stages[id] = i < index ? 'done' : i === index ? 'active' : 'pending'
  })
}

function stageState(message, id) {
  return message.stages?.[id] || 'pending'
}

async function sendMessage() {
  if (busy.value || streaming.value) return
  const promptText = draft.value.trim()
  const image = attachment.value
  const content = [promptText, image?.text?.trim()].filter(Boolean).join('\n\n')
  if (!content) return

  messages.value.push({
    id: createMessageId(),
    role: 'user',
    content,
    imageUrl: image?.imageUrl || ''
  })
  draft.value = ''
  attachment.value = null
  lastResponse.value = null
  lastTrace.value = null
  scrollToBottom()

  if (!supportsEducationTools.value) return sendNonStreaming(content)

  const request = requestState.begin('action')
  busy.value = true
  pendingAction.value = 'chat'
  const assistant = reactive({
    id: createMessageId(),
    role: 'assistant',
    content: '',
    meta: '',
    trace: null,
    _agentType: '',
    streaming: true,
    error: false,
    stages: { identify: 'active', retrieve: 'pending', generate: 'pending' }
  })
  messages.value.push(assistant)
  const controller = new AbortController()
  streamAbort.value = controller
  streaming.value = true
  scrollToBottom()

  try {
    const response = await streamChat(request.backend, request.settings, content, {
      signal: controller.signal,
      onStage: stage => advanceStage(assistant, stage.stage),
      onDelta: text => {
        assistant.content += text
        scrollToBottom()
      }
    })
    if (!request.isCurrent()) return
    if (response.conversationId && response.conversationId !== settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    assistant.content = response.response || assistant.content
    assistant.meta = buildMeta(response)
    assistant._agentType = response.primaryAgent || response.agentType
    assistant.streaming = false
    assistant.stages = { identify: 'done', retrieve: 'done', generate: 'done' }
    lastResponse.value = response
    const trace = await loadToolTrace(response.requestId, request)
    if (request.isCurrent()) {
      lastTrace.value = trace
      assistant.trace = trace?.trace || null
    }
    await Promise.allSettled([loadMonitor(), loadConversations()])
  } catch (error) {
    if (error.name === 'AbortError') {
      if (!assistant.content) assistant.content = '已取消本次回复。'
      assistant.meta = '已取消'
      assistant.streaming = false
      return
    }
    if (!request.isCurrent()) return
    assistant.content = error.message
    assistant.meta = '请求失败'
    assistant.error = true
    assistant.streaming = false
  } finally {
    if (request.isCurrent()) {
      busy.value = false
      pendingAction.value = ''
      streaming.value = false
      streamAbort.value = null
      await nextTick()
      scrollToBottom()
    }
  }
}

async function sendNonStreaming(content) {
  const request = requestState.begin('action')
  busy.value = true
  pendingAction.value = 'chat'
  try {
    const response = await requestChat(request.backend, request.settings, content)
    if (!request.isCurrent()) return
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    const trace = await loadToolTrace(response.requestId, request)
    if (!request.isCurrent()) return
    lastResponse.value = response
    lastTrace.value = trace
    messages.value.push({
      id: createMessageId(),
      role: 'assistant',
      content: response.response,
      meta: buildMeta(response),
      trace: trace?.trace || null,
      _agentType: response.primaryAgent || response.agentType
    })
    await loadMonitor()
  } catch (error) {
    if (!request.isCurrent()) return
    messages.value.push({ id: createMessageId(), role: 'assistant', content: error.message, meta: '请求失败', error: true })
  } finally {
    if (!request.isCurrent()) return
    busy.value = false
    pendingAction.value = ''
    scrollToBottom()
  }
}

function cancelStreaming() {
  if (!streamAbort.value) return
  streamAbort.value.abort()
  streamAbort.value = null
  streaming.value = false
}

async function handleOcrUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  if (!file.type.startsWith('image/')) {
    showToast('请选择作业照片（JPG / PNG / WEBP）')
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    showToast('图片不能超过 8 MB')
    return
  }
  const request = requestState.begin('ocr')
  busy.value = true
  pendingAction.value = 'ocr'
  try {
    const data = await uploadHomeworkImage(request.backend, request.settings, file)
    if (!request.isCurrent()) return
    attachment.value = {
      imageId: data.image_id,
      imageUrl: resolveAssetUrl(request.backend, request.settings, data.image_url),
      text: data.text || '',
      original_name: data.original_name || file.name,
      warning: data.warning || ''
    }
    showToast(data.warning || '已识别题目，请核对后发送')
  } catch (error) {
    if (!request.isCurrent()) return
    showToast(error.message || '图片上传失败')
  } finally {
    if (request.isCurrent()) {
      busy.value = false
      pendingAction.value = ''
    }
  }
}

function usePrompt(prompt) { draft.value = prompt; nextTick(() => composerInput.value?.focus()) }

function clearConversation() {
  cancelStreaming()
  requestState.invalidate()
  busy.value = false
  messages.value = []
  attachment.value = null
  lastResponse.value = null
  lastTrace.value = null
  settings.conversationId = ''
  persist()
}

// ── 会话管理 ──────────────────────────────────────────────────────────────────
function messageFromStored(item) {
  return {
    id: createMessageId(),
    role: item.role,
    content: item.content,
    meta: '',
    trace: null,
    _agentType: ''
  }
}

async function loadConversations(query = historyQuery.value) {
  if (!supportsEducationTools.value) {
    conversations.value = []
    return
  }
  try {
    conversations.value = await listConversations(settings.backend, settings, { query })
  } catch {
    conversations.value = []
  }
}

function openHistory() {
  historyQuery.value = ''
  loadConversations('')
  historyDialog.value?.showModal()
}

function scheduleHistorySearch() {
  clearTimeout(historySearchTimer)
  historySearchTimer = setTimeout(() => loadConversations(historyQuery.value.trim()), 250)
}

async function restoreConversation() {
  if (!settings.conversationId || !supportsEducationTools.value) return
  try {
    const data = await getConversation(settings.backend, settings, settings.conversationId)
    messages.value = (data.messages || []).map(messageFromStored)
    scrollToBottom()
  } catch {
    // 会话过期时保留空白新对话，不阻塞页面。
  }
}

async function openConversation(item) {
  if (busy.value) return
  try {
    const data = await getConversation(settings.backend, settings, item.conv_id)
    requestState.invalidate()
    settings.conversationId = item.conv_id
    persist()
    messages.value = (data.messages || []).map(messageFromStored)
    activeView.value = 'chat'
    historyDialog.value?.close()
    showToast(`已继续「${item.title}」`)
    scrollToBottom()
  } catch {
    showToast('会话加载失败或已过期')
  }
}

function startRename(item) {
  editingConvId.value = item.conv_id
  editingTitle.value = item.title
}

async function confirmRename(item) {
  const title = editingTitle.value.trim()
  if (!title) {
    showToast('标题不能为空')
    return
  }
  try {
    await renameConversation(settings.backend, settings, item.conv_id, title)
    item.title = title
    editingConvId.value = ''
    showToast('会话已重命名')
  } catch (error) {
    showToast(error.message || '重命名失败')
  }
}

async function deleteConversationItem(item) {
  if (!window.confirm(`删除会话「${item.title}」？删除后无法恢复。`)) return
  try {
    await removeConversation(settings.backend, settings, item.conv_id)
    conversations.value = conversations.value.filter(conv => conv.conv_id !== item.conv_id)
    if (settings.conversationId === item.conv_id) clearConversation()
    showToast('会话已删除')
  } catch (error) {
    showToast(error.message || '删除失败')
  }
}

function formatTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// ── 学习闭环 ──────────────────────────────────────────────────────────────────
async function loadLearning() {
  if (!supportsEducationTools.value) return
  try {
    const data = await requestMastery(settings.backend, settings)
    learning.mastery = data.mastery || []
    learning.recent_mistakes = data.recent_mistakes || []
    learning.attempts = data.attempts || 0
    learning.accuracy = data.accuracy || 0
    learning.weakest_topic = data.weakest_topic || ''
  } catch {
    // 后端未启用学习闭环时保持空状态。
  }
}

function latestAssistantId() {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const item = messages.value[index]
    if (item.role === 'assistant' && !item.error && !item.streaming) return item.id
  }
  return ''
}

function isLatestAssistant(item) {
  return latestAssistantId() === item.id
}

function learningTopic() {
  return learningDraft.topic.trim().slice(0, 40)
}

function lastUserQuestion() {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    if (messages.value[index].role === 'user') return messages.value[index].content
  }
  return ''
}

async function requestHintAction(item) {
  if (busy.value) return
  busy.value = true
  pendingAction.value = 'learning'
  try {
    const data = await requestHint(settings.backend, settings, (lastUserQuestion() || item.content).slice(0, 3000), learningTopic())
    messages.value.push({
      id: createMessageId(),
      role: 'assistant',
      content: `**思考提示**\n\n${data.hint}`,
      meta: '学习提示',
      _agentType: '学习提示'
    })
    scrollToBottom()
  } catch (error) {
    showToast(error.message || '提示生成失败')
  } finally {
    busy.value = false
    pendingAction.value = ''
  }
}

async function requestStepsAction(item) {
  if (busy.value) return
  busy.value = true
  pendingAction.value = 'learning'
  try {
    const data = await requestSteps(settings.backend, settings, (lastUserQuestion() || item.content).slice(0, 3000), learningTopic())
    messages.value.push({
      id: createMessageId(),
      role: 'assistant',
      content: data.steps,
      meta: '分步讲解',
      _agentType: '分步讲解'
    })
    scrollToBottom()
  } catch (error) {
    showToast(error.message || '分步讲解生成失败')
  } finally {
    busy.value = false
    pendingAction.value = ''
  }
}

async function requestPracticeAction(item) {
  if (busy.value) return
  busy.value = true
  pendingAction.value = 'learning'
  try {
    const data = await requestPractice(settings.backend, settings, {
      topic: learningTopic(),
      question: (lastUserQuestion() || item.content).slice(0, 3000)
    })
    messages.value.push({
      id: createMessageId(),
      role: 'assistant',
      content: '',
      meta: '同类练习',
      _agentType: '同类练习',
      practice: {
        question: data.question || '',
        answer: data.answer || '',
        explanation: data.explanation || '',
        topic: data.topic || learningDraft.topic || '当前学习内容',
        revealed: false,
        graded: false
      }
    })
    scrollToBottom()
  } catch (error) {
    showToast(error.message || '练习题生成失败')
  } finally {
    busy.value = false
    pendingAction.value = ''
  }
}

async function gradePractice(item, correct) {
  if (item.practice.graded) return
  item.practice.graded = true
  try {
    await recordAttempt(settings.backend, settings, item.practice.topic, correct)
    if (!correct && item.practice.question) {
      // 答错自动进入错题本，复习时可直接回到这道题。
      await addMistake(settings.backend, settings, {
        question: item.practice.question.slice(0, 1500),
        answer: item.practice.answer.slice(0, 4000),
        analysis: item.practice.explanation.slice(0, 2000),
        topic: item.practice.topic.slice(0, 40)
      })
    }
    await loadLearning()
    showToast(correct ? '已记录：掌握度提升' : '已记录：错题已加入错题本')
  } catch {
    showToast('掌握度记录失败，请稍后重试')
  }
}

async function saveMistake(item) {
  if (busy.value) return
  busy.value = true
  pendingAction.value = 'learning'
  try {
    await addMistake(settings.backend, settings, {
      question: (lastUserQuestion() || item.content).slice(0, 1500),
      answer: item.content.slice(0, 4000),
      analysis: '',
      topic: learningTopic()
    })
    await loadLearning()
    showToast('已加入错题本')
  } catch (error) {
    showToast(error.message || '加入错题本失败')
  } finally {
    busy.value = false
    pendingAction.value = ''
  }
}

async function deleteMistakeItem(item) {
  try {
    await removeMistake(settings.backend, settings, item.id)
    learning.recent_mistakes = learning.recent_mistakes.filter(entry => entry.id !== item.id)
    showToast('错题已移除')
  } catch (error) {
    showToast(error.message || '移除失败')
  }
}

function masteryPercent(value) {
  return `${Math.round(Math.max(0, Math.min(Number(value || 0), 1)) * 100)}%`
}

function scrollToComposer() {
  activeView.value = 'chat'
  nextTick(() => composerInput.value?.focus())
}

async function searchKnowledge() {
  if (!searchQuery.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await requestSearch(request.backend, request.settings, searchQuery.value, 5)
    if (!request.isCurrent()) return
    searchResults.value = data.results || []
    searched.value = true
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('检索失败，请检查连接')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function submitKnowledge() {
  if (!docTitle.value.trim() || !docContent.value.trim() || busy.value) return
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await addKnowledge(request.backend, request.settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    showToast('文档已添加')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文档导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || busy.value) return
  if (file.size > 10 * 1024 * 1024) { showToast('文件不能超过 10 MB'); return }
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await uploadKnowledge(request.backend, request.settings, file)
    if (!request.isCurrent()) return
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    if (!request.isCurrent()) return
    showToast(`${file.name} 导入成功`)
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('文件导入失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function runEvaluation() {
  const request = requestState.begin('action')
  busy.value = true
  try {
    const data = await requestEvaluation(request.backend, request.settings, null, {
      isCurrent: request.isCurrent,
      onProgress: job => {
        if (request.isCurrent()) statusText.value = `评测任务 ${job.job_id}：${job.status === 'queued' ? '排队中' : '运行中'}，已完成 ${job.completed}/${job.total} 组`
      }
    })
    if (!request.isCurrent()) return
    evalData.value = data
    showToast(data.status === 'failed' ? '评测执行失败，请查看结果' : data.status === 'partial' ? '评测部分完成，请查看失败原因' : '评测完成')
  } catch (error) {
    if (!request.isCurrent()) return
    statusText.value = error.message
    showToast('评测运行失败')
  } finally { if (request.isCurrent()) busy.value = false }
}

async function loadToolTrace(requestId, request = requestState.begin('trace')) {
  try {
    return await requestToolTrace(request.backend, request.settings, requestId)
  } catch {
    return null
  }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
