<script setup lang="ts">
/**
 * 智能问答页：多轮会话 + 进度 + 结果（表格/SQL/解释/图表）+ 导出。
 *
 * 三个不显然但绕不开的决定：
 *
 * 1. **会话在第一次提问时才创建**，不是在点"新建会话"时。点按钮就建的话，
 *    用户点三次"新建"再离开，列表里会多出三个空会话；而空会话无法区分
 *    "我还没想好问什么"和"我问了但没答上来"。所以"新建会话"只是把当前视图清空，
 *    真正落到后端发生在第一句话发出的时候。
 *
 * 2. **流式过程中不把中间态写进消息列表**，而是由当前这一"轮"对象持有。
 *    中间态一旦进了列表，取消或失败时就得把它们从列表里挑出来清理，
 *    而"哪些是半截的"这件事没有可靠的判据。
 *
 * 3. **结果与图表都从同一份 result 渲染**，切换图表类型不重新提问。
 *    重新问一次要十几秒和一笔 token，而用户只是想换个看法。
 */
import { Delete, Plus, Promotion, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
} from '@/api/conversation'
import { QueryAbortedError, streamQuery } from '@/api/query'
import type {
  ConversationOut,
  DoneEvent,
  ResultSet,
  StreamErrorEvent,
  VizEvent,
} from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import SqlBlock from '@/components/SqlBlock.vue'
import { clearInflight, markInflight, takeInflight } from '@/utils/inflight'
import { formatDateTime, nodeLabel, nodeOrder } from '@/utils/labels'

// 被 DefaultLayout 的 keep-alive 保活，而 include 是**按组件名**匹配的。
// 名字从文件名推出来的那条隐式规则能用，但一旦改名或文件被移动，保活会静默失效
// （表现为"切页面还是会掐断"），所以这里写死。
defineOptions({ name: 'ChatView' })

const route = useRoute()
const router = useRouter()

const MAX_QUERY_CHARS = 500

interface StepState {
  step: string
  status: 'start' | 'done'
}

/** 一问一答合成一轮。与后端 `message` 表的粒度一致（一轮一行）。 */
interface Turn {
  key: string
  question: string
  sql: string | null
  result: ResultSet | null
  explain: string | null
  viz: VizEvent | null
  error: StreamErrorEvent | null
  steps: StepState[]
  done: DoneEvent | null
  streaming: boolean
  createdAt: string | null
}

const conversations = ref<ConversationOut[]>([])
const conversationsLoading = ref(false)

const activeId = ref<string | null>(null)
const turns = ref<Turn[]>([])
const detailLoading = ref(false)

const question = ref('')
const streaming = ref(false)

const scrollBox = ref<HTMLDivElement | null>(null)
let controller: AbortController | null = null
let turnSeq = 0

const currentTurn = computed(() => {
  const last = turns.value[turns.value.length - 1]
  return last?.streaming ? last : null
})

/** 正在跑的步骤（进度条上标"进行中"的那一格）。 */
const runningStep = computed(() => {
  const turn = currentTurn.value
  if (!turn) {
    return null
  }
  const running = [...turn.steps].reverse().find((item) => item.status === 'start')
  return running ? nodeLabel(running.step) : null
})

/**
 * 新的一轮。
 *
 * 必须经 `reactive()` 再交出，不能返回普通对象：流式过程中所有更新（sql / result /
 * explain / progress）都是**在这个对象上原地赋值**，而普通对象进了 `ref` 数组后，
 * 响应式副作用跟踪的是数组读出来的那个代理；持着裸对象改它，值确实变了但没人被通知，
 * 页面会一直停在空白上直到别的地方触发一次重渲染（比如 `streaming` 变 false）。
 * 症状是"进度条不动、结果要等整轮跑完才一次性出现"——看起来像后端没流式，
 * 实际是这个对象没被代理。
 */
function newTurn(questionText: string): Turn {
  turnSeq += 1
  return reactive<Turn>({
    key: `t${turnSeq}`,
    question: questionText,
    sql: null,
    result: null,
    explain: null,
    viz: null,
    error: null,
    steps: [],
    done: null,
    streaming: true,
    createdAt: null,
  })
}

async function scrollToBottom() {
  await nextTick()
  const box = scrollBox.value
  if (box) {
    box.scrollTop = box.scrollHeight
  }
}

/**
 * 拉会话列表。
 *
 * 只取第一页 50 条：侧栏是"最近在用哪些会话"的入口，不是归档浏览器。
 * 后端这个接口也不支持关键字筛选（只有 page / page_size），所以界面上没有搜索框
 * ——摆一个不生效的搜索框，比没有它更糟：用户会以为"搜不到等于没有"。
 */
async function loadConversations() {
  conversationsLoading.value = true
  try {
    const page = await listConversations({ page: 1, page_size: 50 })
    conversations.value = page.items
  } finally {
    conversationsLoading.value = false
  }
}

/**
 * 载入某个会话的历史消息（验收项"重开会话时能加载历史消息"）。
 *
 * 后端把一轮问答存成**一条** role=assistant 的记录（问题与答案同一行），
 * 所以这里每条记录直接还原成一"轮"，不需要再配对。
 */
async function openConversation(id: string) {
  detailLoading.value = true
  try {
    const detail = await getConversation(id)
    activeId.value = detail.id
    turns.value = detail.messages.map((message) => {
      turnSeq += 1
      return {
        key: `h${turnSeq}`,
        question: message.query_text ?? '',
        sql: message.generated_sql,
        result: message.result_preview,
        explain: message.explain_text,
        // 图表类型没有落库（它是渲染选择，不是查询结果），历史消息里由用户自己切
        viz: null,
        error: null,
        steps: [],
        done: null,
        streaming: false,
        createdAt: message.created_at,
      }
    })
    await scrollToBottom()
  } catch {
    // 会话不存在或不属于自己：回到"新会话"状态，而不是停在半截
    activeId.value = null
    turns.value = []
  } finally {
    detailLoading.value = false
  }
}

function startNewConversation() {
  abortStreaming()
  activeId.value = null
  turns.value = []
  question.value = ''
  if (route.name === 'chat-conversation') {
    void router.replace({ name: 'chat' })
  }
}

async function selectConversation(id: string) {
  if (id === activeId.value) {
    return
  }
  abortStreaming()
  if (route.name === 'chat-conversation' && route.params.conversationId === id) {
    await openConversation(id)
    return
  }
  await router.push({ name: 'chat-conversation', params: { conversationId: id } })
}

async function removeConversation(item: ConversationOut) {
  try {
    await ElMessageBox.confirm(`删除会话「${item.title || '未命名'}」及其全部消息？`, '删除会话', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await deleteConversation(item.id)
  ElMessage.success('会话已删除')
  if (activeId.value === item.id) {
    startNewConversation()
  }
  await loadConversations()
}

function abortStreaming() {
  controller?.abort()
  controller = null
  streaming.value = false
}

/**
 * 发送一个问题。
 *
 * 没有活动会话时先建一个——会话 id 就是后端多轮上下文的 `thread_id`
 * （同时用作 checkpointer 的线程），不建的话这次提问是一次性的，
 * 追问时没有任何上文可用（FR-CONV-002）。
 */
async function send() {
  const text = question.value.trim()
  if (!text || streaming.value) {
    return
  }

  let conversationId = activeId.value
  const turn = newTurn(text)
  turns.value.push(turn)
  question.value = ''
  streaming.value = true
  await scrollToBottom()

  try {
    if (!conversationId) {
      // 标题取问题前 50 字：会话列表要的是"一眼认得出是哪一轮"，
      // 整句 256 字塞进侧栏只会被 CSS 截断，看不到有用信息
      const created = await createConversation(text.slice(0, 50))
      conversationId = created.id
      activeId.value = created.id
      await router.replace({ name: 'chat-conversation', params: { conversationId: created.id } })
    }

    controller = new AbortController()
    // 留痕见 utils/inflight：这一轮要是死在刷新上（上下文销毁，下面的 finally
    // 跑不到），下次加载就靠它才知道刚才有一轮没跑完
    markInflight({ conversationId, question: text })
    await streamQuery(
      { query: text, session_id: conversationId },
      {
        onProgress: (event) => {
          turn.steps.push({ step: event.step, status: event.status })
        },
        onSql: (sql) => {
          turn.sql = sql
        },
        onResult: (result) => {
          turn.result = result
        },
        onExplain: (explain) => {
          turn.explain = explain
        },
        onViz: (viz) => {
          turn.viz = viz
        },
        onError: (error) => {
          turn.error = error
        },
        onDone: (done) => {
          turn.done = done
        },
      },
      controller.signal,
    )
  } catch (error) {
    if (error instanceof QueryAbortedError) {
      // 用户主动取消不算失败，但要说清楚这一轮没跑完——否则它看起来像一次
      // 没有结果的正常回答
      turn.error = { code: 0, message: '已取消本次查询' }
    } else {
      turn.error = {
        code: 0,
        message: error instanceof Error ? error.message : '查询失败',
      }
    }
  } finally {
    // 能走到这里就说明页面还活着（成功、报错、用户自己点了停止生成都在内），
    // 那一轮的去向用户已经看见了，不必在下次加载时再提一次。
    // 刷新时走不到这里——这正是留痕能当作"死于刷新"判据的原因。
    clearInflight()
    turn.streaming = false
    streaming.value = false
    controller = null
    await loadConversations()
    await scrollToBottom()
  }
}

function onKeydown(event: KeyboardEvent) {
  // 输入法组字期间的回车是"选字"，不是"发送"。
  // 中文用户用拼音打字，敲回车确认候选词时若被当成发送，会在半句话上发出一次查询——
  // 这是中文界面上最常见的一类误触，而它只在用输入法时出现，测试很容易漏掉。
  if (event.isComposing) {
    return
  }
  // Enter 发送、Shift+Enter 换行
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

function orderedSteps(turn: Turn): StepState[] {
  return [...turn.steps].sort((a, b) => nodeOrder(a.step) - nodeOrder(b.step))
}

function retry(turn: Turn) {
  question.value = turn.question
}

/**
 * 刷新把上一轮掐断之后的善后：告诉用户那一轮没了，并给一次重跑的机会。
 *
 * 判据与理由见 `utils/inflight`——痕迹在=上次提问死于刷新，痕迹被擦掉=上一轮
 * 的去向用户已经看见过了（成功、报错、或自己点了停止生成）。
 *
 * 提示里必须说清"没有保存"：后端中断时连落库都来不及做（落库在流跑完之后），
 * 所以历史里查不到这一轮。不说的话，用户会自己去历史里翻，然后怀疑是不是看漏了。
 */
async function promptInterrupted() {
  const lost = takeInflight()
  if (!lost) {
    return
  }
  const questionText = lost.question.trim()
  const excerpt = questionText.length > 30 ? `${questionText.slice(0, 30)}…` : questionText
  try {
    await ElMessageBox.confirm(
      `「${excerpt}」在页面刷新时被中断，这一轮没有保存。要用同一个问题重新提问吗？`,
      '提问已中断',
      { confirmButtonText: '重新提问', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户选了"取消"，不是错误
  }
  // 不在这里回填 activeId：进这一页之前，框架层已经把路由送到那一轮所在的会话上
  // （见 DefaultLayout 的 onMounted），`openConversation` 成功的话它早已就位。
  // 反过来说，若那条会话已被删掉、载入失败，`activeId` 就是 null——此时照着痕迹里的
  // id 硬填回去，重跑会拿一个不存在的会话去提问，换回一个 404。让它按"新建会话"走，
  // 至少这次提问能正常发出去。
  question.value = questionText
  await send()
}

onMounted(async () => {
  await loadConversations()
  const id = route.params.conversationId
  if (typeof id === 'string' && id) {
    await openConversation(id)
  }
  // 放在载入会话之后：提示里的"重新提问"要接着这一轮的上下文发出去
  await promptInterrupted()
})

watch(
  () => route.params.conversationId,
  async (id) => {
    if (typeof id === 'string' && id && id !== activeId.value) {
      await openConversation(id)
    }
  },
)

// 卸载才掐断：组件真没了（登出、离开主框架）再留着这条连接，是拿一个已经没人看的
// 事件流占着后端一个正在跑的图。
//
// **切到别的页面不会走到这里**：问答页被 keep-alive 保活（见 DefaultLayout），
// 切换只是 deactivate，组件和它手里的事件流都还在，回来接着看。
onBeforeUnmount(abortStreaming)
</script>

<template>
  <div class="chat">
    <!-- 会话侧栏 -->
    <aside class="chat__sessions da-card">
      <div class="chat__sessions-head">
        <el-button type="primary" :icon="Plus" class="chat__new" @click="startNewConversation">
          新建会话
        </el-button>
      </div>
      <div class="chat__sessions-search">
        <el-button link size="small" :icon="Refresh" @click="loadConversations">刷新列表</el-button>
      </div>

      <div class="chat__sessions-list">
        <LoadingBlock v-if="conversationsLoading" :rows="5" />
        <EmptyState
          v-else-if="!conversations.length"
          :image-size="64"
          description="还没有会话"
          hint="在右侧提问，会自动创建会话"
        />
        <ul v-else class="chat__session-items">
          <li
            v-for="item in conversations"
            :key="item.id"
            class="chat__session-item"
            :class="{ 'chat__session-item--active': item.id === activeId }"
            @click="selectConversation(item.id)"
          >
            <div class="chat__session-main">
              <span class="chat__session-title">{{ item.title || '未命名会话' }}</span>
              <span class="chat__session-time">{{ formatDateTime(item.updated_at) }}</span>
            </div>
            <el-button
              class="chat__session-delete"
              link
              :icon="Delete"
              @click.stop="removeConversation(item)"
            />
          </li>
        </ul>
      </div>
    </aside>

    <!-- 对话区 -->
    <section class="chat__main da-card">
      <div ref="scrollBox" class="chat__scroll">
        <LoadingBlock v-if="detailLoading" :rows="6" title />

        <EmptyState
          v-else-if="!turns.length"
          description="问一个数据问题开始"
          hint="例如「去年华东区的销售额是多少？」，可以接着追问「那只看第一季度」"
        />

        <div v-else class="chat__turns">
          <article v-for="turn in turns" :key="turn.key" class="turn">
            <div class="turn__question">
              <span class="turn__badge">问</span>
              <p class="turn__question-text">{{ turn.question }}</p>
            </div>

            <!-- 进度：流式期间逐格点亮，结束后收起成一行摘要。
                 结束后仍然保留（不删），是因为"这一步花了很久 / 这里纠正过 SQL"
                 正是事后复盘要看的，而它只在流式那一刻存在过就太可惜了 -->
            <div v-if="turn.steps.length" class="turn__progress">
              <template v-if="turn.streaming">
                <el-icon class="turn__spin"><Refresh /></el-icon>
                <span class="da-text-sm">正在{{ runningStep || '处理' }}…</span>
              </template>
              <span v-else class="da-text-sm da-text-secondary">
                已完成 {{ orderedSteps(turn).length }} 个步骤<template v-if="turn.done?.correction_count">
                  · SQL 纠正 {{ turn.done.correction_count }} 次</template
                >
              </span>
              <div class="turn__steps">
                <el-tag
                  v-for="item in orderedSteps(turn)"
                  :key="`${item.step}-${item.status}`"
                  size="small"
                  :type="item.status === 'done' ? 'success' : 'primary'"
                  :effect="item.status === 'done' ? 'plain' : 'light'"
                >
                  {{ nodeLabel(item.step) }}
                </el-tag>
              </div>
            </div>

            <el-alert
              v-if="turn.error"
              class="turn__error"
              type="error"
              :closable="false"
              show-icon
              :title="turn.error.message"
            />

            <div v-if="turn.sql" class="turn__sql">
              <SqlBlock :sql="turn.sql" collapsible />
            </div>

            <div v-if="turn.result" class="turn__result">
              <ResultViewer
                :result="turn.result"
                :recommended-type="turn.viz?.chart_type ?? null"
                :viz-message="turn.viz?.message ?? ''"
                :filename="turn.question"
              />
            </div>

            <div v-if="turn.explain" class="turn__explain">
              <div class="turn__section-label">结果解读</div>
              <p class="turn__explain-text">{{ turn.explain }}</p>
            </div>

            <div
              v-if="!turn.streaming && turn.error && !turn.result"
              class="turn__retry"
            >
              <el-button size="small" @click="retry(turn)">重新编辑这个问题</el-button>
            </div>
          </article>
        </div>
      </div>

      <div class="chat__composer">
        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          :maxlength="MAX_QUERY_CHARS"
          show-word-limit
          resize="none"
          placeholder="用一句话描述你要看的数据，Enter 发送、Shift+Enter 换行"
          :disabled="streaming"
          @keydown="onKeydown"
        />
        <div class="chat__composer-actions">
          <span class="da-text-sm da-text-secondary">
            当前会话：{{ activeId ? '已创建，追问会带上文' : '新会话，提问后自动创建' }}
          </span>
          <div class="chat__composer-buttons">
            <el-button v-if="streaming" @click="abortStreaming">停止生成</el-button>
            <el-button
              type="primary"
              :icon="Promotion"
              :loading="streaming"
              :disabled="!question.trim()"
              @click="send"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.chat {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: var(--da-space-4);
  height: 100%;
  min-height: 0;
}

.chat__sessions {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: var(--da-space-3);
}

.chat__sessions-head {
  margin-bottom: var(--da-space-2);
}

.chat__new {
  width: 100%;
}

.chat__sessions-search {
  margin-bottom: var(--da-space-2);
}

.chat__sessions-list {
  flex: 1 1 auto;
  overflow-y: auto;
  min-height: 0;
}

.chat__session-items {
  margin: 0;
  padding: 0;
  list-style: none;
}

.chat__session-item {
  display: flex;
  align-items: center;
  gap: var(--da-space-1);
  padding: var(--da-space-2) var(--da-space-2);
  border-radius: var(--da-radius-md);
  cursor: pointer;
  transition: background var(--da-transition);
}

.chat__session-item:hover {
  background: var(--da-color-bg-hover);
}

.chat__session-item--active {
  background: var(--da-color-primary-light-9);
}

.chat__session-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1 1 auto;
}

.chat__session-title {
  font-size: var(--da-font-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat__session-time {
  font-size: var(--da-font-xs);
  color: var(--da-color-text-placeholder);
}

.chat__session-delete {
  opacity: 0;
  color: var(--da-color-text-secondary);
}

.chat__session-item:hover .chat__session-delete {
  opacity: 1;
}

.chat__main {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0;
}

.chat__scroll {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: var(--da-space-5);
  min-height: 0;
}

.chat__turns {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-5);
}

.turn {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-3);
}

.turn__question {
  display: flex;
  gap: var(--da-space-2);
  align-items: flex-start;
}

.turn__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  margin-top: 2px;
  border-radius: var(--da-radius-sm);
  background: var(--da-color-primary-light-9);
  color: var(--da-color-primary);
  font-size: var(--da-font-xs);
  font-weight: 600;
}

.turn__question-text {
  margin: 0;
  font-size: var(--da-font-md);
  font-weight: 600;
  line-height: var(--da-line-height-tight);
}

.turn__progress {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-2);
  padding: var(--da-space-3);
  background: var(--da-color-bg-soft);
  border-radius: var(--da-radius-md);
}

.turn__steps {
  display: flex;
  flex-wrap: wrap;
  gap: var(--da-space-1);
}

.turn__spin {
  animation: turn-spin 1.2s linear infinite;
  color: var(--da-color-primary);
}

@keyframes turn-spin {
  to {
    transform: rotate(360deg);
  }
}

.turn__section-label {
  margin-bottom: var(--da-space-1);
  font-size: var(--da-font-sm);
  font-weight: 600;
  color: var(--da-color-text-regular);
}

.turn__explain-text {
  margin: 0;
  padding: var(--da-space-3) var(--da-space-4);
  background: var(--da-color-bg-soft);
  border-left: 3px solid var(--da-color-primary-light-5);
  border-radius: var(--da-radius-sm);
  font-size: var(--da-font-base);
}

.chat__composer {
  flex: 0 0 auto;
  padding: var(--da-space-3) var(--da-space-5) var(--da-space-4);
  border-top: 1px solid var(--da-color-border-light);
}

.chat__composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--da-space-3);
  margin-top: var(--da-space-2);
}

.chat__composer-buttons {
  display: flex;
  gap: var(--da-space-2);
}

@media (max-width: 1100px) {
  .chat {
    grid-template-columns: 1fr;
    grid-template-rows: auto minmax(0, 1fr);
  }

  .chat__sessions {
    max-height: 220px;
  }
}
</style>
