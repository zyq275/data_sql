/**
 * "有一轮提问正在跑"的痕迹，以及刷新把它掐断后的善后。
 *
 * **为什么需要它**：刷新页面时浏览器的 JS 上下文整个被销毁，Vue 的
 * `onBeforeUnmount` 不会执行，组件里那份"正在流式"的状态一个字都留不下来；
 * 同时后端那条 SSE 流也会被取消（`StreamingResponse` 在客户端断开时取消任务），
 * 于是这一轮人间蒸发——界面上没了，历史里也查不到，用户还以为是页面卡了。
 *
 * 所以只能在**提问开始时先留痕，正常跑完再擦掉**。刷新回来发现痕迹还在，
 * 就说明上一轮死在了刷新上。判断依据是"痕迹有没有被擦掉"，而不是去问后端：
 * 后端此刻已经什么都没在跑了，问也问不出东西。
 *
 * **为什么是 sessionStorage 而不是 localStorage**：这份痕迹只对"同一个标签页的
 * 下一次加载"有意义。放 localStorage 会让它跨标签页、跨天存活，提示就会在
 * 毫不相干的时刻冒出来——一条"上次提问被中断"的弹窗弹出时，人早就忘了那是哪次。
 */

export interface InflightQuery {
  /** 提问所属会话；没有会话时后端按一次性提问处理，这里也就没有可回填的 id。 */
  conversationId: string | null
  question: string
}

const KEY = 'da.inflight_query'

/**
 * 提问开始时留痕。
 *
 * 存不下不算失败：隐私模式、配额写满时 `sessionStorage` 会抛。留不下痕迹的后果
 * 只是"刷新后不提示"，而为此让一次提问发不出去是本末倒置。
 */
export function markInflight(pending: InflightQuery): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(pending))
  } catch {
    // 见上：静默降级
  }
}

/** 正常结束（成功 / 报错 / 用户主动取消）时擦掉痕迹。 */
export function clearInflight(): void {
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    // 同上
  }
}

/**
 * 读但不擦。给"先判断该把用户送到哪个页面"用——真正的提示由问答页发出，
 * 这一侧只负责看，看的时候就把痕迹擦掉的话，问答页就什么都不剩了。
 */
export function peekInflight(): InflightQuery | null {
  return read()
}

/**
 * 读并擦掉。提示只弹一次：用过就擦，免得它在下一次加载里又冒出来。
 *
 * 内容坏掉就当没有——这是一份提示用的线索，不是事实来源，
 * 为它抛异常或弹一个空问题都不值得。
 */
export function takeInflight(): InflightQuery | null {
  const pending = read()
  if (pending) {
    clearInflight()
  }
  return pending
}

function read(): InflightQuery | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw) as Partial<InflightQuery>
    return typeof parsed?.question === 'string' ? (parsed as InflightQuery) : null
  } catch {
    return null
  }
}
