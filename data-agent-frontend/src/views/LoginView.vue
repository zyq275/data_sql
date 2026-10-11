<script setup lang="ts">
/**
 * 登录页（深色 HUD 控制台）。
 *
 * 形态上是一块悬浮的玻璃面板，左边是品牌与"这个产品到底在干什么"的实时演示，
 * 右边是表单。之所以不做成常见的"左蓝右白"硬分栏：白的这一半除了一个表单
 * 什么都没有，一整屏的空白只能靠留白撑住，撑不住就显廉价。深色底 + 一块
 * 有边界的面板，等于给空白找了个边界，两边也终于是同一个东西的两半。
 *
 * 视觉语言取自 HUD / Sci-Fi FUI（细线、发光、透明层、等宽字、数据感），
 * 但**克制地只取三样**：透视网格地平线、扫描线、玻璃面板 + 四角括号。
 * 全息的青色/洋红那一路没要——那是科幻电影的道具感，
 * 这是个要天天用的内部系统，登录页不该比业务页更像游戏。
 *
 * 颜色全部从 token 派生（主色 / chart-4 / chart-5），所以验收 K2 那条
 * "改 --da-color-primary 全站变色"依然成立：登录页跟着一起变。
 * 登录页专用的深色变量写在 <style scoped> 的 `.login` 里而不是 tokens.css ——
 * 那是全站共享文件，为一个页面的深色皮肤动它会波及所有业务页。
 */

import { Lock, User } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

/* ============================================================
 * 一、左侧的实时演示
 *
 * 登录页上最值得"演"的不是动效，是产品本身：一句话进去，SQL 出来。
 * 所以这里循环播放三段真实的问答（题目取自项目的评测样本，不是编的），
 * 逐字打字 → 停顿"思考" → 逐行吐出 SQL → 报校验结果。
 *
 * 整个演示是装饰性的，对读屏隐藏（`aria-hidden`）：一串会自己变化的
 * 假 SQL 对读屏用户是纯噪音，而且它会不停打断"请使用系统分配的账号登录"
 * 这句真正要读的话。产品能力已经用下面那三个标签用正常文字讲清楚了。
 * ============================================================ */

interface Demo {
  /** 自然语言提问 */
  q: string
  /** 生成的 SQL，一行一条 */
  sql: string[]
  /** 校验结论 */
  meta: string
}

const DEMOS: Demo[] = [
  {
    q: '华东大区一共有多少个省份？',
    sql: [
      'SELECT COUNT(province) AS 省份数',
      'FROM dim_region',
      "WHERE region_name = '华东'",
    ],
    meta: '命中 1 行 · 0.8s',
  },
  {
    q: '最近三个月各品类的销售额排名',
    sql: [
      'SELECT category, SUM(amount) AS 销售额',
      'FROM fact_order',
      'WHERE dt >= CURRENT_DATE - INTERVAL 3 MONTH',
      'GROUP BY category',
      'ORDER BY 销售额 DESC LIMIT 10',
    ],
    meta: '命中 7 行 · 1.2s',
  },
  {
    q: '会员等级一共有几档？',
    sql: ['SELECT COUNT(DISTINCT member_level) AS 档数', 'FROM dim_customer'],
    meta: '命中 1 行 · 0.6s',
  },
]

/** 演示进行到哪一步。文案与结构化 SQL 的显隐都由它决定。 */
type Phase = 'ask' | 'think' | 'sql' | 'done'

const demoIndex = ref(0)
/** 已打出来的提问字数 */
const typed = ref('')
/** 已显示的 SQL 行数 */
const sqlLines = ref(0)
const phase = ref<Phase>('ask')

const current = computed(() => DEMOS[demoIndex.value])
/* 补零到两位。演示只有三段，但补零之后读起来像仪表的读数而不是"1/3" */
const step = computed(() => String(demoIndex.value + 1).padStart(2, '0'))
const total = computed(() => String(DEMOS.length).padStart(2, '0'))

/* ---- SQL 语法高亮 ----
 * 关键字 / 字符串 / 数字三类分别上色。用正则切出 token 再 `v-for` 渲染，
 * **不用 `v-html`**：哪怕内容是这里写死的常量，一个能拼 HTML 的出口放在
 * 登录页上也毫无必要——这是全站唯一一个不需要鉴权就能打开的页面。
 *
 * 匹配顺序即优先级：字符串在最前，否则 `'华东'` 里的引号会被后面的规则拆开。
 */
const TOKEN_RE =
  /('(?:[^']*)')|(\b\d+\b)|(\b(?:SELECT|FROM|WHERE|GROUP BY|ORDER BY|LIMIT|AS|COUNT|SUM|DISTINCT|CURRENT_DATE|INTERVAL|MONTH|AND|DESC|ASC)\b)/g

interface Token {
  text: string
  /** '' | 'kw' | 'str' | 'num' */
  kind: string
}

function highlight(line: string): Token[] {
  const out: Token[] = []
  let cursor = 0
  for (const match of line.matchAll(TOKEN_RE)) {
    const at = match.index ?? 0
    if (at > cursor) {
      out.push({ text: line.slice(cursor, at), kind: '' })
    }
    out.push({ text: match[0], kind: match[1] ? 'str' : match[2] ? 'num' : 'kw' })
    cursor = at + match[0].length
  }
  if (cursor < line.length) {
    out.push({ text: line.slice(cursor), kind: '' })
  }
  return out
}

/* 常量输入，切一次就够，别每帧重算 */
const HIGHLIGHTED: Token[][][] = DEMOS.map((demo) => demo.sql.map(highlight))

/* ---- 播放 ----
 * 用**一个**定时器句柄串起整条时间线，而不是给每一步各存一个 id：
 * 任何时刻只可能有一个待执行的步骤，存一个就够，清理时也不可能漏。
 */
let timer = 0

function later(step: () => void, ms: number) {
  timer = window.setTimeout(step, ms)
}

function clearTimer() {
  if (timer) {
    clearTimeout(timer)
    timer = 0
  }
}

/* 读取一次就不再变：系统偏好运行期不会改，而每次都读 matchMedia 会强制样式计算 */
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches

function play(index: number) {
  const demo = DEMOS[index]
  demoIndex.value = index
  typed.value = ''
  sqlLines.value = 0
  phase.value = 'ask'

  /* 减少动效：直接把第一段问答的**终态**铺出来，不播放、也不循环。
     这是演示，不是状态指示——没有它用户不会看不到任何东西，
     而一个自己在那儿打字的东西正是前庭敏感用户最想关掉的那类动画。 */
  if (REDUCED_MOTION) {
    typed.value = demo.q
    sqlLines.value = demo.sql.length
    phase.value = 'done'
    return
  }

  const think = () => {
    phase.value = 'think'
    later(reveal, 640)
  }

  const reveal = () => {
    phase.value = 'sql'
    let shown = 0
    const step = () => {
      shown += 1
      sqlLines.value = shown
      if (shown < demo.sql.length) {
        later(step, 140)
      } else {
        phase.value = 'done'
        later(() => play((index + 1) % DEMOS.length), 3000)
      }
    }
    step()
  }

  const type = () => {
    // 一次一个字符。42ms 是"像在敲"和"看得完"之间的位置：
    // 再快就成了整句闪现，再慢读的人会等得不耐烦。
    const next = typed.value.length + 1
    typed.value = demo.q.slice(0, next)
    if (next < demo.q.length) {
      later(type, 42)
    } else {
      later(think, 460)
    }
  }

  later(type, 260)
}

/**
 * 切到后台就停掉。
 *
 * 这个演示是登录页上唯一"自己一直在动"的东西，而用户切走之后它除了
 * 烧 CPU 和耗电没有任何意义——标签页不可见时浏览器虽然会降频，
 * 但定时器仍然会跑，每跑一次都要改 DOM、触发一轮渲染。
 */
function onVisibilityChange() {
  if (document.hidden) {
    clearTimer()
  } else {
    play(demoIndex.value)
  }
}

onMounted(() => {
  play(0)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

/* ============================================================
 * 二、跟手的光斑
 *
 * 位置存的是**视口坐标的像素**，写进 CSS 变量、由 CSS 的 transform 消费。
 * 不在 JS 里直接改 style.transform 是因为：变量能参与 CSS 里的过渡，
 * 于是"跟手"自带一点拖尾，鼠标一停光斑还在缓缓贴上去；
 * 而每次 pointermove 都直接写 transform 是没有过渡可言的，动起来很生硬。
 * ============================================================ */

/* 光斑用视口坐标。面板是居中的，光斑落在视口里就够了，
   不需要相对某个元素换算——之前那版要减去面板的 left，纯属自找麻烦。 */
const spot = reactive({ x: window.innerWidth * 0.34, y: window.innerHeight * 0.42 })

/** 不跟手时的落点：偏左上，和面板错开，不落在表单正后方 */
const SPOT_REST = { x: 0.34, y: 0.42 }

const spotlight = computed(() => ({
  '--spot-x': `${spot.x}px`,
  '--spot-y': `${spot.y}px`,
}))

let frame = 0

/**
 * 一帧只算一次。pointermove 在 120Hz 的屏幕上每秒能来上百次，
 * 每次都读坐标再写变量，等于让浏览器每帧算好几遍。
 */
function trackPointer(event: PointerEvent) {
  if (frame) {
    return
  }
  // 先把坐标取出来：事件对象在 rAF 回调里已经派发完了，别在回调里读它
  const { clientX, clientY } = event
  frame = requestAnimationFrame(() => {
    frame = 0
    spot.x = clientX
    spot.y = clientY
  })
}

/** 鼠标离开窗口，光斑缓缓滑回落点（过渡在 CSS 里，这里只管目标值）。 */
function resetSpot() {
  spot.x = window.innerWidth * SPOT_REST.x
  spot.y = window.innerHeight * SPOT_REST.y
}

onBeforeUnmount(() => {
  clearTimer()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  // 组件在动画帧落地前被卸载的话，回调里那次赋值就没必要了
  if (frame) {
    cancelAnimationFrame(frame)
    frame = 0
  }
})

/* ============================================================
 * 三、表单
 * ============================================================ */

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { max: 128, message: '用户名最长 128 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度 6-32 位', trigger: 'blur' },
  ],
}

/**
 * 校验没过时，把光标送到第一个出错的输入框。
 *
 * 表单已经会把红字显示在出错字段下面，但焦点还停在"登录"按钮上——
 * 键盘用户按完回车，页面看起来毫无反应（错误信息在他身后），得自己 Tab 回去找。
 * 这属于 WCAG「出错后把焦点交给第一个无效字段」，也是纯键盘操作的前提。
 *
 * `nextTick` 那一步不能省：`is-error` 这个类是校验结果出来后由 Vue 渲染上去的，
 * 同一 tick 里查 DOM 会查到上一帧的样子，什么都选不中。
 */
async function focusFirstError(formEl: FormInstance) {
  await nextTick()
  // `$el` 在类型上是 any，不能带泛型参数，只能断言返回值
  const input = formEl.$el?.querySelector('.el-form-item.is-error input') as
    | HTMLInputElement
    | null
    | undefined
  input?.focus()
}

async function submit() {
  const formEl = formRef.value
  if (!formEl) {
    return
  }
  const valid = await formEl.validate().catch(() => false)
  if (!valid) {
    await focusFirstError(formEl)
    return
  }

  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    // 回到被拦下的那一页；没有来源就去问答页
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' && redirect ? redirect : { name: 'chat' })
  } catch (error) {
    // 错误提示已由 http 拦截器统一弹出（分不清用户名错还是密码错是后端有意为之：
    // 40100 的 message 是"用户名或密码错误"这类不区分两者的文案）
    form.password = ''
    void error
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div
    class="login"
    data-theme="light"
    :style="spotlight"
    @pointermove.passive="trackPointer"
    @pointerleave="resetSpot"
  >
    <!--
      上面那个 `data-theme` 是这一页的主题开关：`light`（默认）/ `dark`。
      深浅两版是同一组 `--lg-*` 变量的两套取值，所以切主题只是改这一个词，
      不用碰任何一条样式规则——两版也就不存在"改了一版忘了另一版"的问题。
      想跟系统偏好联动，把它换成 `:data-theme="prefersDark ? 'dark' : 'light'"` 即可。
    -->
    <!--
      背景层。全部 `aria-hidden` + `pointer-events: none`：
      对读屏不可见，也不会挡住文案的选中、光标的移动、表单的点击。

      **必须包在一个 fixed + overflow:hidden 的壳里**，不能让它们直接挂在
      `.login` 底下：`.login` 纵向是 `overflow-y: auto`，而这些图层是故意超出
      容器的（极光 `inset: -30%`，往下多铺 270px）。绝对定位的子元素一样算进
      滚动高度——实测把 `.login` 的 scrollHeight 从 900 撑到了 1163，
      于是页面上凭空多出一条滚动条，居中也被挤偏 4px。
      壳用 `fixed` 而不是 `absolute`：滚动时背景不会跟着跑，始终贴住视口。

      z 序从下到上：底色 → 极光 → 透视网格 → 扫描线 → 光斑 → （壳外）面板。
      扫描线**放在面板之下**是刻意的：它是给玻璃面板"垫"纹理用的，
      压在最上面就成了一层糊在文字上的灰膜，小字号会明显发虚。
    -->
    <div class="login__decor" aria-hidden="true">
      <div class="login__bg" />
      <div class="login__aurora" />
      <div class="login__grid" />
      <div class="login__scan" />
      <div class="login__spot" />

      <!-- 四角的定位括号。HUD 的记号，纯装饰 -->
      <span class="login__bracket login__bracket--tl" />
      <span class="login__bracket login__bracket--tr" />
      <span class="login__bracket login__bracket--bl" />
      <span class="login__bracket login__bracket--br" />
    </div>

    <main class="console">
      <!-- 顶栏：三个点 + 标识 + 右侧状态灯。它让下面那块玻璃像"一个终端窗口"
           而不是一张卡片，是整页科技感最省力的一笔。 -->
      <header class="console__bar">
        <span class="console__lights" aria-hidden="true">
          <i /><i /><i />
        </span>
        <span class="console__path">DATA AGENT · NL2SQL CONSOLE</span>
        <span class="console__status">
          <i class="console__pulse" aria-hidden="true" />
          在线
        </span>
      </header>

      <div class="console__body">
        <!-- ---- 左：品牌与实时演示 ---- -->
        <section class="intro">
          <div class="intro__brand">
            <span class="intro__mark">数</span>
            <span class="intro__name">数据通达</span>
          </div>

          <h1 class="intro__headline">用一句话<br />问出你要的数据</h1>
          <p class="intro__desc">
            自然语言提问，自动理解指代、检索口径、生成并校验 SQL，结果附带 SQL 与解释，可看图、可导出。
          </p>

          <div class="term" aria-hidden="true">
            <div class="term__head">
              <span class="term__label">实时演示</span>
              <span class="term__index">{{ step }} / {{ total }}</span>
            </div>

            <div class="term__body">
              <p class="term__ask">
                <span class="term__caret-mark">&gt;</span>
                <span>{{ typed }}</span>
                <span v-if="phase === 'ask'" class="term__caret" />
              </p>

              <p v-if="phase === 'think'" class="term__think">
                <span class="term__spin" />解析意图 · 检索口径 · 生成 SQL
              </p>

              <pre v-else-if="sqlLines" class="term__sql"><code
                v-for="(line, i) in HIGHLIGHTED[demoIndex].slice(0, sqlLines)"
                :key="i"
                class="term__line"
              ><span
                v-for="(token, j) in line"
                :key="j"
                :class="token.kind && `term__tok--${token.kind}`"
              >{{ token.text }}</span></code></pre>

              <p v-if="phase === 'done'" class="term__meta">
                <span class="term__ok">✓</span> 校验通过 · {{ current.meta }}
              </p>
            </div>
          </div>

          <ul class="intro__points">
            <li>多轮追问，指代自动补全</li>
            <li>SQL 与解释一并给出</li>
            <li>按角色管控数据权限</li>
          </ul>
        </section>

        <!-- ---- 右：表单 ---- -->
        <section class="panel">
          <div class="panel__inner">
            <h2 class="panel__title">登录</h2>
            <p class="panel__subtitle">请使用系统分配的账号登录</p>

            <el-form
              ref="formRef"
              class="panel__form"
              :model="form"
              :rules="rules"
              size="large"
              label-position="top"
              @submit.prevent="submit"
            >
              <el-form-item label="用户名" prop="username">
                <!-- autocomplete 是给浏览器和密码管理器看的（WCAG 2.2 AA 要求登录页
                     必须支持自动填充与粘贴）：少了它，用户每次都得手打，
                     而"不许粘贴"这类限制恰恰是那条准则明令禁止的。 -->
                <el-input
                  v-model="form.username"
                  :prefix-icon="User"
                  placeholder="请输入用户名"
                  autocomplete="username"
                />
              </el-form-item>

              <el-form-item label="密码" prop="password">
                <el-input
                  v-model="form.password"
                  type="password"
                  show-password
                  :prefix-icon="Lock"
                  placeholder="请输入密码"
                  autocomplete="current-password"
                  @keyup.enter="submit"
                />
              </el-form-item>

              <el-button
                type="primary"
                size="large"
                class="panel__submit"
                :loading="loading"
                @click="submit"
              >
                登录
              </el-button>
            </el-form>

            <p class="panel__foot">内部系统 · 仅限授权账号访问</p>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<style scoped>
/* ============================================================
 * 登录页的主题变量
 *
 * 这一页自带一套外观参数，深浅两版只是**同一组变量的两套取值**。
 * 下面所有具体规则只引用 `--lg-*`，没有一条写死颜色——所以切主题
 * 不用动任何一条规则，只换变量块，两版才不会随时间慢慢长歪。
 * 切换方式：根元素的 `data-theme`（见模板），默认 light。
 *
 * 颜色仍然从 --da-* token 派生，不新造色值：改 --da-color-primary，
 * 这一页的网格、玻璃、按钮、焦点框会一起换色（验收 K2）。
 * chart-4 / chart-5 是设计系统里本来就有的紫与青，拿来当第二、第三色相，
 * 避免整页只有一种蓝——单色相会显得很"塑料"。
 *
 * `color-mix` 用得多，是**双向**的：深色底上要把 token 里的紫/青/橙提亮
 * （它们当初是按浅色底挑的，直接压在近黑上会发闷），浅色底上反过来要压暗
 * （那些色在白底上对比度只有 3 左右，够不上 4.5:1）。
 * 每处都写两遍声明做渐进增强——不认 color-mix 的浏览器停在上一行的原色上。
 * ============================================================ */
.login {
  /* ---- 浅色底（默认）---- */
  --lg-bg: var(--da-color-bg-page);
  --lg-line: rgba(31, 36, 48, 0.08);
  --lg-line-strong: rgba(31, 36, 48, 0.15);
  --lg-text: var(--da-color-text-primary);
  --lg-dim: var(--da-color-text-regular);
  /* 最浅的这一档是**正文色**，不是装饰色（终端标题、状态行、脚注都用它），
     所以它必须自己就够 4.5:1。
     注意**不能直接用 `--da-color-text-secondary`**：那个灰在浅底上只有
     3.5:1（它在业务页里当副标题用，是那批页面既有的问题，不归这里管）。
     和正文色混一档之后约 6.5:1，视觉上仍然是"次要的灰"，但读得清。 */
  --lg-faint: color-mix(in srgb, var(--da-color-text-secondary) 55%, var(--da-color-text-primary));
   /* 同上：全局的 placeholder 色在白底上只有 2.2:1，太浅了 */
  --lg-placeholder: color-mix(
    in srgb,
    var(--da-color-text-placeholder) 55%,
    var(--da-color-text-primary)
  );

  --lg-accent: var(--da-color-primary);
  --lg-accent-strong: var(--da-color-primary);

  /* SQL 三类 token。浅底上都要压暗才够对比度 */
  --lg-kw: var(--da-color-primary);
  --lg-str: color-mix(in srgb, var(--da-color-warning) 62%, #1a1400);
  --lg-num: color-mix(in srgb, var(--da-chart-4) 78%, #120a2a);
  --lg-ok: color-mix(in srgb, var(--da-color-success) 70%, #04231a);

  /* 背景层。
     这些 alpha 是**实拍调出来的**，不是按手感写的：浅底上同一个透明度
     的视觉强度远低于深底（白上加一点蓝≈看不出，黑上加一点蓝≈一道光），
     第一版照 0.1 那档数值铺下去，极光和网格在截图里直接消失了，
     于是这一页只剩"干净的浅色表单"，HUD 的骨架没了。
     现在这几档比深色版还高一点，是补这个差值。 */
  --lg-bg-tint-1: rgba(var(--da-color-primary-rgb), 0.17);
  --lg-bg-tint-2: rgba(var(--da-chart-5-rgb), 0.14);
  --lg-aurora-1: rgba(var(--da-color-primary-rgb), 0.32);
  --lg-aurora-2: rgba(var(--da-chart-4-rgb), 0.28);
  --lg-aurora-3: rgba(var(--da-chart-5-rgb), 0.24);
  /* 网格线在浅底上必须**往深里走**，不能沿用深色版那套"把主色提亮当线"。
     线看不看得见取决于和底色的**亮度差**，不是色相差：深色版里浅蓝比近黑底亮，
     一眼就能看见；浅色版里浅蓝比浅底还亮，等于没画。
     所以这里混进正文色压成深海军蓝，再由 --lg-grid-opacity 统一控强度——
     一个变量管颜色、一个管浓淡，比把 alpha 写死在颜色里更好调。 */
  --lg-grid-line: color-mix(in srgb, var(--da-color-primary) 52%, var(--da-color-text-primary));
  --lg-grid-opacity: 0.5;
  /* 扫描线是唯一**不能**跟着一起加的：它是叠在小字上的纹理，
     浅底上压到 0.03 以上就开始像"屏幕脏了"，而且会啃横笔画的边缘。 */
  --lg-scan: rgba(31, 36, 48, 0.022);
  --lg-spot: radial-gradient(
    circle,
    rgba(var(--da-color-primary-rgb), 0.16),
    rgba(var(--da-chart-5-rgb), 0.06) 46%,
    transparent 68%
  );
  --lg-bracket: rgba(var(--da-color-primary-rgb), 0.5);

  /* 玻璃面板。浅色底上的玻璃必须靠**极淡的描边 + 大而软的投影**立起来，
     不能学深色版用发光——白底上的光晕只会把面板边缘糊掉。 */
  --lg-glass: rgba(255, 255, 255, 0.6);
  --lg-glass-border: rgba(31, 36, 48, 0.09);
  --lg-glass-highlight: rgba(255, 255, 255, 0.9);
  --lg-glass-shadow:
    0 28px 70px rgba(23, 33, 56, 0.14),
    0 2px 6px rgba(23, 33, 56, 0.05),
    0 0 90px rgba(var(--da-color-primary-rgb), 0.1);

  --lg-bar-bg: rgba(var(--da-color-primary-rgb), 0.045);
  --lg-term-bg: rgba(255, 255, 255, 0.75);
  --lg-panel-bg: rgba(255, 255, 255, 0.45);

  /* 小面积的光。浅底上的光要收着用：同样的 alpha 在白底上比在黑底上刺眼得多 */
  --lg-lights-off: rgba(31, 36, 48, 0.16);
  --lg-glow-strong: rgba(var(--da-color-primary-rgb), 0.42);
  --lg-glow-soft: rgba(var(--da-color-primary-rgb), 0.28);
  --lg-success-glow: rgba(23, 166, 115, 0.5);

  /* 表单控件 */
  --lg-input-bg: rgba(255, 255, 255, 0.9);
  --lg-input-bg-focus: #ffffff;
  --lg-input-border: rgba(31, 36, 48, 0.16);
  --lg-input-border-hover: rgba(31, 36, 48, 0.32);
  --lg-focus-ring: rgba(var(--da-color-primary-rgb), 0.16);
  --lg-btn-shadow: rgba(var(--da-color-primary-rgb), 0.28);
  --lg-btn-shadow-hover: rgba(var(--da-color-primary-rgb), 0.42);
  --lg-btn-shadow-active: rgba(var(--da-color-primary-rgb), 0.3);

  --lg-scrollbar: rgba(31, 36, 48, 0.18);
  --lg-scrollbar-hover: rgba(31, 36, 48, 0.32);

  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  min-height: 100dvh;
  padding: var(--da-space-6);
  /* 横向必须裁掉：网格和极光都是按超出视口的宽度画的，不裁就会出现横向滚动条。
     纵向反过来要能滚——笔记本竖屏（视口高不到 700px）上面板会顶出去，
     `overflow: hidden` 会把登录按钮直接切掉，那就不是"不好看"而是"登不进去"了。 */
  overflow-x: hidden;
  overflow-y: auto;
  background: var(--lg-bg);
  color: var(--lg-text);
  font-family: var(--da-font-family);
}

/* ============================================================
 * 深色底：同一组变量的另一套取值
 *
 * 浅色版是「极淡描边 + 大而软的投影」，深色版反过来是「细亮描边 + 辉光」——
 * 白底和黑底要把同一块玻璃"抬起来"，用的物理语言本来就不一样，
 * 所以这两块里变量的**数量**是一样的，差别只落在取值上。
 *
 * 大面积的透明度在这里普遍调高：同一个 alpha，压在白底上比压在近黑上
 * 视觉强度高一截，直接照搬浅色的数值会得到一版"灰蒙蒙"的深色。
 * ============================================================ */
.login[data-theme='dark'] {
  --lg-bg: #04060f;
  --lg-line: rgba(255, 255, 255, 0.09);
  --lg-line-strong: rgba(255, 255, 255, 0.16);
  --lg-text: #eef2ff;
  --lg-dim: rgba(226, 233, 255, 0.72);
  --lg-faint: rgba(226, 233, 255, 0.55);
  --lg-placeholder: rgba(226, 233, 255, 0.55);

  --lg-accent: var(--da-color-primary-light-3);
  --lg-accent-strong: var(--da-color-primary);

  /* token 里的紫/青/橙是按浅色底挑的，压在近黑上会发闷，这里统一提亮 */
  --lg-kw: var(--da-chart-5);
  --lg-kw: color-mix(in srgb, var(--da-chart-5) 58%, #ffffff);
  --lg-str: var(--da-color-warning);
  --lg-str: color-mix(in srgb, var(--da-color-warning) 62%, #ffffff);
  --lg-num: var(--da-chart-4);
  --lg-num: color-mix(in srgb, var(--da-chart-4) 74%, #ffffff);
  --lg-ok: var(--da-color-success);

  --lg-bg-tint-1: rgba(var(--da-color-primary-rgb), 0.16);
  --lg-bg-tint-2: rgba(var(--da-chart-5-rgb), 0.1);
  --lg-aurora-1: rgba(var(--da-color-primary-rgb), 0.34);
  --lg-aurora-2: rgba(var(--da-chart-4-rgb), 0.28);
  --lg-aurora-3: rgba(var(--da-chart-5-rgb), 0.2);
  --lg-grid-line: var(--da-color-primary);
  --lg-grid-opacity: 0.3;
  --lg-scan: rgba(255, 255, 255, 0.03);
  --lg-spot: radial-gradient(
    circle,
    rgba(var(--da-color-primary-rgb), 0.22),
    rgba(var(--da-chart-5-rgb), 0.06) 42%,
    transparent 68%
  );
  --lg-bracket: rgba(var(--da-color-primary-rgb), 0.5);

  --lg-glass: rgba(10, 14, 28, 0.62);
  --lg-glass-border: rgba(255, 255, 255, 0.16);
  --lg-glass-highlight: rgba(255, 255, 255, 0.07);
  --lg-glass-shadow:
    0 40px 90px rgba(0, 0, 0, 0.6),
    0 0 120px rgba(var(--da-color-primary-rgb), 0.16);

  --lg-bar-bg: rgba(255, 255, 255, 0.03);
  --lg-term-bg: rgba(4, 7, 16, 0.6);
  --lg-panel-bg: rgba(255, 255, 255, 0.02);

  --lg-lights-off: rgba(255, 255, 255, 0.16);
  --lg-glow-strong: rgba(var(--da-color-primary-rgb), 0.9);
  --lg-glow-soft: rgba(var(--da-color-primary-rgb), 0.55);
  --lg-success-glow: rgba(23, 166, 115, 0.85);

  --lg-input-bg: rgba(255, 255, 255, 0.035);
  --lg-input-bg-focus: rgba(255, 255, 255, 0.055);
  --lg-input-border: rgba(255, 255, 255, 0.16);
  --lg-input-border-hover: rgba(255, 255, 255, 0.3);
  --lg-focus-ring: rgba(var(--da-color-primary-rgb), 0.2);
  --lg-btn-shadow: rgba(var(--da-color-primary-rgb), 0.32);
  --lg-btn-shadow-hover: rgba(var(--da-color-primary-rgb), 0.5);
  --lg-btn-shadow-active: rgba(var(--da-color-primary-rgb), 0.34);

  --lg-scrollbar: rgba(255, 255, 255, 0.16);
  --lg-scrollbar-hover: rgba(255, 255, 255, 0.3);
}

/* 万一真的滚起来（矮屏），滚动条也得跟着主题走。
   base.css 里那条全局规则给的是固定的浅灰 thumb，落在深色版上
   像一道没擦干净的刮痕。这两个选择器只作用于本页。 */
.login::-webkit-scrollbar-thumb {
  background: var(--lg-scrollbar);
}

.login::-webkit-scrollbar-thumb:hover {
  background: var(--lg-scrollbar-hover);
}

/* 装饰层的壳。见模板里的说明：`overflow: hidden` 是这里的关键，
   没有它，故意超出的极光和网格会把 `.login` 撑出一条滚动条。 */
.login__decor {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  /* 透视挂在**壳上**，而不是写进 grid 的 transform 里——这一处差别决定了网格
     到底在不在屏幕上。写成 `transform: perspective(460px) rotateX(64deg)` 时，
     消失点跟着 `transform-origin` 走，也就是元素的底边中点，而底边被
     `bottom: -14%` 推到了屏幕下方。684px 高的平面于是朝那个点塌过去，
     实测只投影出 128px 高、顶边落在 y=898——视口是 900，屏幕上剩 2px。
     挂在父层上时消失点是父层中心（屏幕中线），地平线落在画面中部，
     网格才真的是"铺在地上"的。这个 bug 在两个主题下都在，
     只是深色版那 2px 的残影被遮罩淡掉了，一直没现形。 */
  perspective: 700px;
}

/* ---- 底色与暗角 ----
 * 中间偏上提亮、四角压暗：面板浮在亮区上，边缘自然融进底色里，
 * 不至于像贴在一张纯色纸上。（浅色版这套就成了"中间更白、四角更灰"。） */
.login__bg {
  position: absolute;
  inset: 0;
  z-index: 0;
  background:
    radial-gradient(120% 90% at 50% 8%, var(--lg-bg-tint-1), transparent 62%),
    radial-gradient(90% 70% at 12% 96%, var(--lg-bg-tint-2), transparent 60%),
    var(--lg-bg);
  pointer-events: none;
}

/* ---- 极光 ----
 * 两块大色斑缓慢反向漂移。不用 `filter: blur()` 做柔化：模糊一个上千像素的层
 * 代价很高，而 radial-gradient 本身就是软的，边缘天然没有硬边。
 * 动的是 `transform` 而不是 `background-position`：后者每帧都要重画整层，
 * 前者在合成层上做，几乎不要钱。
 *
 * 22s 一个来回，慢到几乎察觉不到"在动"，只留下"这页是活的"的印象。
 */
.login__aurora {
  position: absolute;
  inset: -30%;
  z-index: 1;
  background-image:
    radial-gradient(38% 42% at 26% 26%, var(--lg-aurora-1), transparent 68%),
    radial-gradient(32% 36% at 78% 30%, var(--lg-aurora-2), transparent 68%),
    radial-gradient(40% 40% at 62% 82%, var(--lg-aurora-3), transparent 70%);
  animation: lg-drift 22s ease-in-out infinite alternate;
  pointer-events: none;
}

@keyframes lg-drift {
  from {
    transform: translate3d(-2%, -1%, 0) scale(1);
  }

  to {
    transform: translate3d(3%, 2%, 0) scale(1.12);
  }
}

/* ---- 透视网格地平线 ----
 * 整页的"科技感"有一半来自它。做法是把一层横竖细线用 `perspective + rotateX`
 * 压成贴地的平面，再用 mask 从下往上淡出——不淡出的话网格会一直铺到面板背后，
 * 和面板的边框打架。
 *
 * 动 `background-position` 在这里是划算的：网格在线框模式下几乎是空的，
 * 重绘的就那么几十条 1px 线，而且它带来的"在向观察者滚动"的观感是
 * 平移一整块色斑换不来的。
 */
.login__grid {
  position: absolute;
  right: -40%;
  bottom: -14%;
  left: -40%;
  z-index: 2;
  height: 76%;
  background-image:
    linear-gradient(to right, var(--lg-grid-line) 1px, transparent 1px),
    linear-gradient(to bottom, var(--lg-grid-line) 1px, transparent 1px);
  background-size: 64px 64px;
  opacity: var(--lg-grid-opacity);
  /* 透视在父层（见 .login__decor）。这里只负责"把平面放倒" */
  transform: rotateX(64deg);
  transform-origin: 50% 100%;
  mask-image: linear-gradient(to top, #000 0%, transparent 68%);
  -webkit-mask-image: linear-gradient(to top, #000 0%, transparent 68%);
  animation: lg-scroll 4.2s linear infinite;
  pointer-events: none;
}

@keyframes lg-scroll {
  to {
    background-position: 0 64px;
  }
}

/* ---- 扫描线 ----
 * 3px 一个周期的极浅横纹，模拟 CRT。透明度必须压到很低（0.03 量级）：
 * 再高一点，小字号的横笔画就会被切出缺口，读起来发虚。
 * 它铺在**面板之下**，玻璃的 backdrop-filter 会把它揉进去当纹理。 */
.login__scan {
  position: absolute;
  inset: 0;
  z-index: 3;
  background: repeating-linear-gradient(to bottom, var(--lg-scan) 0 1px, transparent 1px 3px);
  pointer-events: none;
}

/* ---- 跟手光斑 ----
 * 位置由 JS 写进 `--spot-x/--spot-y`。位移用 `translate` 而不是 `left/top`：
 * 改 left/top 会触发重排，而这里每帧都在变。
 * 半径用负 margin 校正中心，而不是在 transform 里再跟一个 `translate(-50%,-50%)`——
 * 那样过渡会把"居中"也一起当成动画量，光标一停光斑还会往左上滑一段。 */
.login__spot {
  position: absolute;
  left: 0;
  top: 0;
  z-index: 4;
  width: 720px;
  height: 720px;
  margin: -360px 0 0 -360px;
  border-radius: var(--da-radius-pill);
  background: var(--lg-spot);
  transform: translate(var(--spot-x, 0px), var(--spot-y, 0px));
  /* 跟手但带一点拖尾：光标停下后光斑还会缓缓贴上去，
     这是"跟随"和"黏在鼠标上"的区别 */
  transition: transform 0.5s var(--da-ease-out);
  will-change: transform;
  pointer-events: none;
}

/* ---- 四角括号 ---- */
.login__bracket {
  position: absolute;
  z-index: 5;
  width: 26px;
  height: 26px;
  border: 1px solid var(--lg-bracket);
  pointer-events: none;
  /* 从角上"长出来"：进场时用 clip-path 划开，是这一页唯一一处
     有 HUD 意味的入场动作。 */
  animation: lg-draw 0.7s var(--da-ease-out) 0.2s both;
}

@keyframes lg-draw {
  from {
    clip-path: inset(0 100% 100% 0);
    opacity: 0;
  }

  to {
    clip-path: inset(0 0 0 0);
    opacity: 1;
  }
}

.login__bracket--tl {
  top: 22px;
  left: 22px;
  border-right: 0;
  border-bottom: 0;
}

.login__bracket--tr {
  top: 22px;
  right: 22px;
  border-bottom: 0;
  border-left: 0;
}

.login__bracket--bl {
  bottom: 22px;
  left: 22px;
  border-top: 0;
  border-right: 0;
}

.login__bracket--br {
  right: 22px;
  bottom: 22px;
  border-top: 0;
  border-left: 0;
}

/* ============================================================
 * 控制台面板
 * ============================================================ */
.console {
  position: relative;
  z-index: 10;
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 1060px;
  overflow: hidden;
  border: 1px solid var(--lg-glass-border);
  border-radius: var(--da-radius-lg);
  /* 玻璃：半透明底 + 背景模糊。透明度不能太高（0.62 上下封顶），
     否则背后的网格线会穿过文字，等宽小字会糊成一团。
     `inset 0 1px 0` 是顶边上的一道内高光，专门用来暗示"这是一块有厚度的玻璃"；
     投影响应主题（黑底靠辉光浮起来，白底靠投影压下去）。 */
  background: var(--lg-glass);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  box-shadow:
    inset 0 1px 0 var(--lg-glass-highlight),
    var(--lg-glass-shadow);
  /* 入场：缩放 + 上浮。只有一次，且只动 opacity/transform——
     动画第一帧起输入框就已经可以点，不阻塞操作。 */
  animation: lg-in 0.55s var(--da-ease-out) both;
}

@keyframes lg-in {
  from {
    opacity: 0;
    transform: translateY(14px) scale(0.985);
  }

  to {
    opacity: 1;
    transform: none;
  }
}

.console__bar {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  flex: 0 0 auto;
  padding: 0 var(--da-space-5);
  height: 46px;
  border-bottom: 1px solid var(--lg-line);
  background: var(--lg-bar-bg);
  font-family: var(--da-font-family-mono);
  font-size: var(--da-font-xs);
}

.console__lights {
  display: inline-flex;
  gap: 7px;
}

.console__lights i {
  width: 10px;
  height: 10px;
  border-radius: var(--da-radius-pill);
  background: var(--lg-lights-off);
}

/* 只有第一颗点亮着主色：三颗一样亮就只是装饰，一颗有颜色才像"这个窗口在用" */
.console__lights i:first-child {
  background: var(--lg-accent-strong);
  box-shadow: 0 0 10px var(--lg-glow-strong);
}

.console__path {
  flex: 1 1 auto;
  overflow: hidden;
  color: var(--lg-faint);
  letter-spacing: 0.14em;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.console__status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
  color: var(--lg-dim);
}

.console__pulse {
  width: 7px;
  height: 7px;
  border-radius: var(--da-radius-pill);
  background: var(--da-color-success);
  box-shadow: 0 0 8px var(--lg-success-glow);
  animation: lg-pulse 2.4s ease-in-out infinite;
}

@keyframes lg-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }

  50% {
    opacity: 0.35;
    transform: scale(0.8);
  }
}

.console__body {
  display: grid;
  grid-template-columns: 1.08fr 0.92fr;
  min-height: 566px;
}

/* ============================================================
 * 左：品牌与实时演示
 * ============================================================ */
.intro {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--da-space-4);
  padding: var(--da-space-6);
}

/* 中缝的分隔线用**渐隐**的竖线而不是实线或 border：实线会把面板切成两个盒子，
   渐隐线只暗示"这里有分区"，主体仍然是一整块玻璃。
   也不用 `border-image` 实现——那东西和 border-radius / overflow 的相互作用
   在几个浏览器上表现不一致，一个 1px 的装饰线不值得为它踩坑。 */
.intro::after {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 1px;
  background: linear-gradient(to bottom, transparent, var(--lg-line-strong), transparent);
  pointer-events: none;
}

/* 入场：逐个错开 60ms。位移只有 12px——距离越大越像"飞进来"，
   那是弹窗的语言，不是页面的。`both` 保证动画开始前就停在起始帧，
   不会先闪一下再动。 */
@keyframes lg-rise {
  from {
    opacity: 0;
    transform: translateY(12px);
  }

  to {
    opacity: 1;
    transform: none;
  }
}

.intro > * {
  animation: lg-rise 0.5s var(--da-ease-out) both;
}

.intro > :nth-child(1) {
  animation-delay: 0.1s;
}

.intro > :nth-child(2) {
  animation-delay: 0.16s;
}

.intro > :nth-child(3) {
  animation-delay: 0.22s;
}

.intro > :nth-child(4) {
  animation-delay: 0.28s;
}

.intro > :nth-child(5) {
  animation-delay: 0.34s;
}

.intro__brand {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
}

/* 和顶栏 DefaultLayout 的品牌块同款：同一枚标记在登录页和业务页里长得一样，
   用户才不会觉得自己进了两个系统。 */
.intro__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--da-radius-sm);
  background: linear-gradient(135deg, var(--da-color-primary), var(--da-color-primary-active));
  color: var(--da-color-text-inverse);
  font-weight: 600;
  box-shadow: 0 0 18px var(--lg-glow-soft);
}

.intro__name {
  font-size: var(--da-font-md);
  font-weight: 600;
  letter-spacing: 0.04em;
}

.intro__headline {
  margin: 0;
  font-size: var(--da-font-display);
  font-weight: 600;
  line-height: var(--da-line-height-tight);
  letter-spacing: 0.01em;
}

.intro__desc {
  margin: 0;
  max-width: 34em;
  font-size: var(--da-font-base);
  line-height: var(--da-line-height-base);
  color: var(--lg-dim);
}

/* ---- 演示终端 ---- */
.term {
  display: flex;
  flex-direction: column;
  min-height: 208px;
  margin-top: auto;
  border: 1px solid var(--lg-line);
  border-radius: var(--da-radius-md);
  background: var(--lg-term-bg);
  overflow: hidden;
}

.term__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--da-space-4);
  height: 32px;
  border-bottom: 1px solid var(--lg-line);
  font-family: var(--da-font-family-mono);
  font-size: var(--da-font-xs);
  color: var(--lg-faint);
  letter-spacing: 0.12em;
}

.term__index {
  color: var(--lg-faint);
}

.term__body {
  flex: 1 1 auto;
  padding: var(--da-space-4);
  font-family: var(--da-font-family-mono);
  font-size: var(--da-font-sm);
  line-height: 1.75;
}

.term__ask {
  margin: 0;
  display: flex;
  gap: var(--da-space-2);
  color: var(--lg-text);
}

.term__caret-mark {
  flex: 0 0 auto;
  color: var(--lg-accent);
}

/* 光标：跟着打字位置走。它只在"还在打字"时出现，所以静止时是干净的 */
.term__caret {
  display: inline-block;
  width: 7px;
  height: 1.05em;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--lg-accent);
  animation: lg-blink 1.05s steps(1, end) infinite;
}

@keyframes lg-blink {
  50% {
    opacity: 0;
  }
}

.term__think {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
  margin: var(--da-space-3) 0 0;
  color: var(--lg-faint);
}

/* 思考中的转圈。和 base.css 里 EP 加载图标是同一个道理：
   这是状态指示，不是装饰，减少动效时也不该停（见文件末尾的媒体查询）。 */
.term__spin {
  width: 12px;
  height: 12px;
  flex: 0 0 auto;
  border: 1.5px solid var(--lg-line-strong);
  border-top-color: var(--lg-accent);
  border-radius: var(--da-radius-pill);
  animation: lg-spin 0.7s linear infinite;
}

@keyframes lg-spin {
  to {
    transform: rotate(360deg);
  }
}

.term__sql {
  margin: var(--da-space-3) 0 0;
  padding: 0;
  border: 0;
  background: none;
  color: var(--lg-dim);
  font: inherit;
  white-space: pre-wrap;
  word-break: break-word;
}

.term__line {
  display: block;
  /* 每行落地时从左侧淡入，方向正好是"一行行生成"的阅读方向 */
  animation: lg-line 0.28s var(--da-ease-out) both;
}

@keyframes lg-line {
  from {
    opacity: 0;
    transform: translateX(-6px);
  }

  to {
    opacity: 1;
    transform: none;
  }
}

/* SQL 三类 token 的颜色。两套主题下对比度都在 5:1 以上（各自对着自己的
   终端底色算过），小字号等宽字也读得清——
   这些色不能直接拿 token 原色来用：同一支橙，在近黑底上要提亮才够，
   在白底上反而要压暗才够。 */
.term__tok--kw {
  color: var(--lg-kw);
}

.term__tok--str {
  color: var(--lg-str);
}

.term__tok--num {
  color: var(--lg-num);
}

.term__meta {
  margin: var(--da-space-3) 0 0;
  color: var(--lg-dim);
  animation: lg-rise 0.3s var(--da-ease-out) both;
}

.term__ok {
  color: var(--lg-ok);
}

.intro__points {
  display: flex;
  flex-wrap: wrap;
  gap: var(--da-space-2) var(--da-space-4);
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--da-font-sm);
  color: var(--lg-faint);
}

/* 用一个小方块代替 list-style 的圆点：圆点是"文档"的标点，
   方块更像"仪表上的指示灯"，和这一页的语言一致 */
.intro__points li {
  display: flex;
  align-items: center;
  gap: 7px;
}

.intro__points li::before {
  content: '';
  width: 5px;
  height: 5px;
  flex: 0 0 auto;
  background: var(--lg-accent);
  box-shadow: 0 0 6px var(--lg-glow-strong);
}

/* ============================================================
 * 右：表单
 * ============================================================ */
.panel {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--da-space-6);
  /* 比左半边亮一档：右半边是"要动手的地方"，得比读稿的左半边更靠前。
     不用更亮的边框去区分，那会让它看起来像另一个盒子。
     （浅色版同理，只是"亮一档"的方向从"更白"变成了"更纯的白"。） */
  background: var(--lg-panel-bg);
}

.panel__inner {
  width: 100%;
  max-width: 340px;
  animation: lg-rise 0.5s var(--da-ease-out) 0.22s both;
}

.panel__title {
  margin: 0;
  font-size: var(--da-font-xl);
  font-weight: 600;
}

.panel__subtitle {
  margin: var(--da-space-1) 0 var(--da-space-5);
  font-size: var(--da-font-sm);
  color: var(--lg-dim);
}

/* ---- 把 EP 的表单控件接到这一页的配色上 ----
 * 全部走 EP 自己暴露的 `--el-*` 变量，不去覆盖它的类名：
 * 类名会随 EP 版本变，变量不会。这些值只在这个组件里生效（scoped），
 * 不会漏到业务页。
 *
 * **变量必须挂在 `.el-input` 自己身上，不能只挂在 `.panel__form` 上**：
 * EP 在它的 input.css 里把这几个 `--el-input-*` 声明在了 `.el-input` 元素上，
 * 而"元素自身的自定义属性"永远压过"从祖先继承来的"。挂在祖先上的结果是——
 * 一点都不生效，输入框仍然是 `--el-fill-color-blank` 的纯白/纯色，
 * 于是底色和字色撞在一起、图标也看不见。这个坑不报错，只在截图里现形。
 * 这里的 `:deep(.el-input)` 比 EP 的 `.el-input` 多两级，稳定压过它。
 *
 * placeholder 单走 `--lg-placeholder`，不复用正文那几档灰：
 * 它需要的对比度和正文字号不一样，实测 4.5:1 上下才既"是提示"又读得出。 */
.panel__form {
  --el-text-color-regular: var(--lg-text);
  --el-text-color-placeholder: var(--lg-placeholder);
}

.panel__form :deep(.el-input) {
  --el-input-bg-color: var(--lg-input-bg);
  --el-input-border-color: var(--lg-input-border);
  --el-input-hover-border-color: var(--lg-input-border-hover);
  --el-input-focus-border-color: var(--lg-accent);
  --el-input-text-color: var(--lg-text);
  --el-input-placeholder-color: var(--lg-placeholder);
  --el-input-icon-color: var(--lg-faint);
  --el-input-clear-hover-color: var(--lg-dim);
}

.panel__form :deep(.el-form-item__label) {
  padding-bottom: var(--da-space-1);
  font-size: var(--da-font-sm);
  color: var(--lg-dim);
  transition: color var(--da-duration-fast) var(--da-ease-out);
}

/* 必填星号换上主色：EP 默认的红色在这块深底上既刺眼、又和"出错"的红撞了 */
.panel__form :deep(.el-form-item.is-required .el-form-item__label::before) {
  color: var(--lg-accent);
}

.panel__form :deep(.el-form-item) {
  margin-bottom: var(--da-space-5);
}

.panel__form :deep(.el-input__wrapper) {
  padding: 10px 14px;
  border-radius: var(--da-radius-md);
  transition:
    box-shadow var(--da-duration-base) var(--da-ease-out),
    background-color var(--da-duration-base) var(--da-ease-out);
}

/* 聚焦：内圈一道主色实线 + 外圈一层主色柔光。用 `:focus-within` 而不是
   EP 的 `.is-focus`——前者是 CSS 标准，不依赖 EP 在哪种输入框上加哪个类。 */
.panel__form :deep(.el-form-item:focus-within .el-input__wrapper) {
  background: var(--lg-input-bg-focus);
  box-shadow:
    0 0 0 1px var(--lg-accent) inset,
    0 0 0 4px var(--lg-focus-ring);
}

/* 光标落进哪个输入框，那个框的图标和标签一起变主色。
   走 EP 的 `--el-input-icon-color` 变量而不是覆盖 `.el-input__icon` 的类名。 */
.panel__form :deep(.el-form-item:focus-within) {
  --el-input-icon-color: var(--lg-accent);
}

.panel__form :deep(.el-form-item:focus-within .el-form-item__label) {
  color: var(--lg-accent);
}

/* ---- 登录按钮 ---- */
.panel__submit {
  position: relative;
  width: 100%;
  margin-top: var(--da-space-1);
  overflow: hidden;
  /* 把边框涂成透明而不是 `border: 0`：去掉边框会让按钮矮 2px，
     而 EP 的尺寸是按有边框算的，改高度会顺带影响加载图标的居中 */
  border-color: transparent;
  /* 渐变压在 EP 的 bg-color 之上（background-image 叠在 background-color 上），
     所以不用 !important 就能既保留 EP 的变量体系、又拿到渐变。 */
  background-image: linear-gradient(120deg, var(--da-color-primary), var(--da-color-primary-active));
  box-shadow: 0 8px 24px var(--lg-btn-shadow);
  transition:
    box-shadow var(--da-duration-base) var(--da-ease-out),
    transform var(--da-duration-fast) var(--da-ease-out);
}

/* `:not(.is-disabled)`：EP 的禁用态仍会命中 :hover，不加这个的话
   按钮加载中（禁用）时还会亮起来，看着像"还能再点一次"。 */
.panel__submit:not(.is-disabled):hover {
  background-image: linear-gradient(120deg, var(--da-color-primary-hover), var(--da-color-primary));
  box-shadow: 0 10px 30px var(--lg-btn-shadow-hover);
  transform: translateY(-1px);
}

/* 按下时收回去一点。按压反馈（0.95-1.05 这个量级）是"按到了"最直接的确认，
   比颜色变化更早被感知到。 */
.panel__submit:not(.is-disabled):active {
  transform: translateY(0) scale(0.985);
  box-shadow: 0 4px 14px var(--lg-btn-shadow-active);
}

/*
 * 一道斜向扫光。平时停在按钮外面（translateX(-140%)），只在悬停时扫过去一次——
 * 静止状态不留痕迹，所以它不会把按钮变成一个看起来在呼吸的控件。
 * 用 `::after` 是因为 EP 的加载遮罩占的是 `::before`，不抢。
 */
.panel__submit::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    100deg,
    transparent 32%,
    rgba(255, 255, 255, 0.36) 50%,
    transparent 68%
  );
  transform: translateX(-140%);
  pointer-events: none;
}

.panel__submit:not(.is-disabled):hover::after {
  transform: translateX(140%);
  transition: transform 0.7s var(--da-ease-out);
}

.panel__foot {
  margin: var(--da-space-5) 0 0;
  font-family: var(--da-font-family-mono);
  font-size: var(--da-font-xs);
  color: var(--lg-faint);
  letter-spacing: 0.06em;
}

/* ============================================================
 * 响应式
 * ============================================================ */

/* 窄一点的桌面：收到单列，演示终端留下（它才是这一页的主体），
   卖点标签让位——单列里再堆三行标签会把表单推到首屏之外。 */
@media (max-width: 1000px) {
  .console__body {
    grid-template-columns: 1fr;
    min-height: 0;
  }

  .intro {
    gap: var(--da-space-3);
    padding: var(--da-space-5);
    padding-bottom: var(--da-space-4);
  }

  /* 单列之后，中缝从"右边一条竖线"变成"下边一条横线" */
  .intro::after {
    top: auto;
    right: 0;
    bottom: 0;
    left: 0;
    width: auto;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--lg-line-strong), transparent);
  }

  /* 单列之后纵向空间变成稀缺资源，每一处都得让一点：
     标题收到 xl，说明段和卖点标签整个收掉，终端的内边距减半。
     加在一起是八十来像素——正好是"登录按钮在首屏里"和"要滚一下"的差别。
     一个得滚动才看得到登录按钮的登录页，比少两句介绍糟糕得多。 */
  .intro__headline {
    font-size: var(--da-font-xl);
  }

  .intro__desc,
  .intro__points {
    display: none;
  }

  .term {
    margin-top: var(--da-space-1);
    min-height: 0;
  }

  .term__body {
    padding: var(--da-space-3) var(--da-space-4);
  }

  .panel {
    padding: var(--da-space-5);
  }
}

/* 矮屏（1366×768 这类笔记本）。面板总高 612px 是按"屏幕还有富余"定的，
   扣掉浏览器地址栏和任务栏之后视口只剩 650px 上下，整块就贴边了。
   这里把面板压扁而不是让它滚——滚一下才能看到登录按钮的登录页，
   第一次用的人很可能以为下面没有了。 */
@media (max-height: 780px) and (min-width: 1001px) {
  .console__body {
    min-height: 468px;
  }

  .term {
    min-height: 172px;
  }

  .intro,
  .panel {
    padding: var(--da-space-5);
  }
}

/* 手机。
   演示终端整个收掉：4.5 寸的屏上同时摆一段会自己打字的 SQL 和登录框，
   两样都读不进去，而且会把登录按钮顶出首屏。 */
@media (max-width: 640px) {
  .login {
    padding: var(--da-space-4);
  }

  .term {
    display: none;
  }

  .console__body {
    display: block;
  }

  /* 顶栏的路径文字在这个宽度上会被截成 "DATA AGENT · NL2SQL C…"，
     半截话比没有更难看。三个圆点和状态灯本来就是这一栏的正文。 */
  .console__path {
    display: none;
  }
}

/* ============================================================
 * 尊重系统的"减少动态效果"
 *
 * 全局那条规则（styles/base.css）已经把动画时长压到近乎为零，
 * 这里补三处它管不到的：
 *   1. 持续循环的动效要**整个停掉**（压到 0.01ms 会让它停在某一帧上
 *      继续占着合成层）；
 *   2. 跟手光斑改成直接贴住光标、不留拖尾；
 *   3. 演示终端里的转圈是状态指示、不是装饰，放行（和 base.css 里
 *      EP 加载图标同一个例外理由）。
 * 演示的播放逻辑本身也在 script 里读了同一个偏好，直接铺终态、不循环。
 * ============================================================ */
@media (prefers-reduced-motion: reduce) {
  .login__aurora,
  .login__grid,
  .console__pulse,
  .term__caret {
    animation: none;
  }

  .login__spot {
    transition: none;
  }

  .console__pulse {
    opacity: 1;
    transform: none;
  }

  .term__caret {
    opacity: 1;
  }

  .term__spin {
    animation-duration: 0.9s !important;
    animation-iteration-count: infinite !important;
  }

  .panel__submit:not(.is-disabled):hover {
    transform: none;
  }
}
</style>
