# 数据通达 — 前端（data-agent-frontend）

Vue 3 + TypeScript + Element Plus + ECharts。开发期通过 Vite 代理访问后端，
生产由 Nginx 承担同样的转发（TDD 14.1，并在 Nginx 侧关闭 SSE 缓冲）。

## 环境与运行

```bash
npm install
npm run dev      # http://localhost:5173，固定端口（strictPort）
npm run build    # vue-tsc 类型检查 + vite build，产物在 dist/
npm run preview  # 预览构建产物
```

**端口固定为 5173**，与后端 `conf/app_config.yaml` 的 `cors_origins` 白名单一一对应。
端口被占用时直接报错而不是自动换端口——换了端口，实际来源就与白名单不一致，
而症状（CORS 报错）离原因（换了个端口）很远。

后端需同时在 5173 之外的位置运行（默认 8000）；`vite.config.ts` 把 `/api` 与 `/health`
代理过去。

## 目录结构

```
src/
├── api/           接口层：http（信封拆解/令牌/续期）、query（SSE）、各业务模块
├── stores/        Pinia：auth（登录态与角色）
├── router/        路由与守卫（登录校验 + 角色门槛）
├── layouts/       DefaultLayout：侧栏 + 顶栏 + 内容区
├── components/    EmptyState / LoadingBlock / EChart / SqlBlock / ResultViewer
├── views/         十个页面
├── utils/         storage / role / labels / csv / chart / echarts
└── styles/        tokens.css（设计 Token）+ base.css
```

## 设计系统

**先有 Token，再有页面。** 页面里只写 `var(--da-*)`，不写死色值、圆角、阴影
（开发步骤文档"常见坑"表点名的一条：配色散乱）。改主题只需改
[tokens.css](src/styles/tokens.css) 一处。

唯一的字面色值在 `utils/chart.ts` 的 `tokenColor(name, fallback)` 第二参数上。
那不是漏网的写死颜色，是"CSS 变量读不到"时的兜底（详见函数注释）。
按这条自查时可以跑：`src/` 下搜 `#[0-9a-fA-F]`，命中应当**只在 `utils/chart.ts`**。

- 前缀用 `da-`（data-agent），避开 Element Plus 的 `--el-*`；
- 文件末尾把 `--el-color-primary` 等映射到 `--da-*`，让 EP 组件与自研部分共用一套色
  ——不做这层映射，就会出现"按钮是 EP 默认蓝、旁边标签是自己那套蓝"的两套蓝并存；
- 图表调色板（`--da-chart-1..6`）也在 Token 里，JS 侧经 `utils/chart.ts` 的
  `tokenColor()` 读取，不在代码里重写一份。同一个指标在问答页和看板页是同一个颜色。

**样式加载顺序有语义**（`main.ts`）：EP 样式 → tokens.css → base.css。
把 tokens.css 提到 EP 之前，映射会被 EP 的默认值覆盖，全局只剩默认蓝。

## 页面与角色

| 路由 | 页面 | 最低角色 |
|---|---|---|
| `/login` | 登录（分栏式） | — |
| `/chat`、`/chat/:conversationId` | 智能问答 | viewer |
| `/history` | 查询历史 | viewer |
| `/dashboard` | 数据看板 | **analyst** |
| `/profile` | 个人设置 | viewer |
| `/admin/users` | 用户管理 | **admin** |
| `/admin/permissions` | 权限管理 | **admin** |
| `/admin/eval` | 评测看板 | **admin** |
| `/403` | 无权访问 | 任意已登录 |

角色门槛写在路由 `meta.requiresRole` 上，侧栏菜单**由同一份数据过滤生成**
（`DefaultLayout` 的 `GROUPS`），加页面时不会漏判角色。

**这是体验层的拦截，不是安全边界**：真正的拒绝在后端依赖（`require_admin` /
`require_min_role`，返回 40300）。前端的判断错了，后果是"某个入口该藏没藏"，
不是"越权拿到了数据"。

反向也拦一道：**已登录的人打开 `/login` 会被送回问答页**（`?redirect=` 一并带走）。
判断用的是 `restore()` 而不是 store 里的 `user`——刷新页面后 `user` 是空的、令牌还在，
只看 `user` 会得出"没登录"，于是把已登录的用户留在登录页上。

## 接口层

### `api/http.ts`

1. **拆信封**：后端一律返回 `{code, message, data}`，业务代码只看见 `data`。
   非信封的响应（探针 `/health`）原样返回。
2. **带令牌**：每个请求自动附 `Authorization: Bearer`。
3. **续期**：**只有业务码 40101（令牌过期）**触发 refresh 并重放原请求。
   40100（凭据不对，登录页输错密码就是它）与 40102（令牌无效）不去 refresh——
   前者会把"用户刚输错的密码"变成莫名其妙的登录状态变化，后者救不回来。
   并发请求一起过期时共享同一个 refresh Promise，否则多个 refresh 同时在飞，
   后到的会被先到的作废（症状：刷新页面偶尔被踢回登录页）。

`onUnauthorized` 用注册回调而不是让 http.ts 直接 `import router`：
router → 守卫 → auth store → api/auth → api/http 会绕成一个环。

令牌的事实来源是 localStorage（`utils/storage.ts`），store 里的 `user` 只是缓存；
刷新页面后要重新用令牌换一次 `/auth/me`。单独拆出 `utils/storage.ts` 同样是为了断环。

### `api/query.ts`（SSE）

**不用原生 `EventSource`**：它只支持 GET，而后端的问答接口是 **POST**——问题原文要走
请求体（塞进 query string 会进浏览器历史与访问日志）。这里用 `fetch` +
`ReadableStream` 自己解析 SSE 帧。

解析时必须**按空行切帧**，不能按单个换行：一帧内部本来就有换行（`event:` 行 +
`data:` 行），按换行切会把每行都当成一帧。

后端每帧的 `data` 里都带 `type`，与 `event:` 行同值；解析以 `data.type` 为准，
`event:` 行只作兜底。

## 几处刻意的取舍

- **会话在第一次提问时才创建**：点"新建会话"只清空当前视图。点三次再离开不会留下
  三个空会话，而空会话无法区分"还没想好问什么"和"问了但没答上来"。
- **图表切换不重新提问**：后端只推荐类型（bar/line/pie/table），不给字段映射
  （`visualization.py` 的 `Recommendation` 只有 `chart_type` 和 `message`），
  所以前端按"这列是不是数字"自己分维度与度量——与后端判"该不该画折线"用的是同一套
  朴素规则。
- **看板的默认图表类型是前端猜的**：`DashboardMetricsResponse` 里没有图表推荐字段，
  与问答链路不同。这里按同样的规则给个初始值，用户随时可切；一个默认值判错，
  代价是一次点击，而默认停在表格上，代价是"看板页有 ECharts 却看不到图"。
- **CSV 导出带 BOM 并做公式注入防护**：不带 BOM 的 UTF-8 CSV 在中文 Windows + Excel
  上必然是乱码；以 `=` / `+` / `@` 开头的单元格会被 Excel 当公式执行，而数据来自数仓。
  `-` 单独处理：负数是正常数据，只在"减号后面不是数字"时才拦。
- **Enter 发送要看 `isComposing`**：输入法组字期间的回车是"选字"不是"发送"。
  中文用户敲回车确认候选词时若被当成发送，会在半句话上发出一次查询。
- **`.env` 不进前端**：前端没有任何密钥，`VITE_API_BASE_URL` 是唯一的环境变量，
  且只影响请求前缀。

## 已知取舍

- Element Plus 走**全局注册**（`app.use(ElementPlus)`），打包出的
  `element-plus` 块约 988 kB（gzip 318 kB），首屏就下载。换取的是零额外构建依赖、
  不会出现"某个组件的样式没被引入"这类难查的问题。若将来在意首屏，可换成
  `unplugin-vue-components` 按需引入，但要多两个构建期依赖。
- 构建块已按 `echarts` / `element-plus` / 业务代码分开（`vite.config.ts`），
  改业务代码不会让用户重下依赖块；echarts 只在用图表的页面被拉取。

## 阶段 12 验收对照

手工核对清单见上级 README 的「验证清单（阶段 12 验收）」。
`npm run build` 通过＝类型与构建无误，**不等于**交互正确——上面那些取舍都只有
在浏览器里点一遍才看得出来。
