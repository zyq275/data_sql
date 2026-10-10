/**
 * 结果集导出为 CSV。
 *
 * 两处不写就会出问题的地方：
 *
 * 1. **BOM**。Excel 打开不带 BOM 的 UTF-8 CSV 会按本地编码解释，中文全变乱码。
 *    这不是"偶尔"——在中文 Windows + Excel 上是必然，而导出功能的第一受众就是
 *    要把结果丢进 Excel 的人。加一个 U+FEFF 就能避开。
 *
 * 2. **公式注入**。CSV 在 Excel 里被解析时，以 `=` / `+` / `@` 开头的单元格会被当公式。
 *    数据来自数仓，值里出现 `=cmd|'/c calc'!A1` 这种是可能的（客户名、备注字段）。
 *    导出功能于是成了把别人的数据变成本机命令执行的通道。惯例的处置是加一个前导
 *    单引号，Excel 会把它当作"这是文本"而不显示该引号。
 *
 *    `-` 单独处理：负数是正常数据，一律加引号会让所有负数在 Excel 里变成文本、
 *    无法参与计算。所以只在"减号后面不是数字"时才当可疑值——`-5` 放过，`-1+2` 拦下。
 */

/** 需要加引号前缀的起始字符。 */
const RISKY_PREFIXES = ['=', '+', '@', '\t', '\r']

/**
 * 字节序标记 U+FEFF。
 *
 * 用码点构造而不是直接写这个字符：它是一个**看不见**的字符，写在源码里一旦被编辑器
 * 或格式化工具顺手清掉，没有任何报错、没有任何 diff 上的异常，只有用户导出的中文
 * 全变乱码时才会发现。`String.fromCharCode(0xfeff)` 在源码里是可见的 ASCII。
 */
const BOM = String.fromCharCode(0xfeff)

function isNumericLike(value: string): boolean {
  return /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(value)
}

function sanitize(value: unknown): string {
  if (value === null || value === undefined) {
    return ''
  }
  const text = typeof value === 'string' ? value : String(value)
  if (!text) {
    return ''
  }
  const first = text[0]!
  if (RISKY_PREFIXES.includes(first)) {
    return `'${text}`
  }
  if (first === '-' && !isNumericLike(text)) {
    return `'${text}`
  }
  return text
}

function escapeCell(value: unknown): string {
  const text = sanitize(value)
  // 含分隔符、引号或换行的字段要用双引号包起来，内部的引号翻倍
  if (/[",\n\r]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`
  }
  return text
}

export function toCsv(columns: string[], rows: unknown[][]): string {
  const lines = [columns.map(escapeCell).join(',')]
  for (const row of rows) {
    lines.push(row.map(escapeCell).join(','))
  }
  return lines.join('\r\n')
}

/** 触发浏览器下载。文件名里的时间戳让多次导出不会互相覆盖。 */
export function downloadCsv(filename: string, columns: string[], rows: unknown[][]): void {
  const content = BOM + toCsv(columns, rows)
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename.endsWith('.csv') ? filename : `${filename}.csv`
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

/** 用问题原文生成一个还认得出是什么的文件名（去掉路径与非法字符）。 */
export function safeFilename(text: string, fallback = 'query-result'): string {
  const cleaned = text
    .replace(/[\\/:*?"<>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 40)
  return cleaned || fallback
}
