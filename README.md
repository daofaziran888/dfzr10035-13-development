# 网关工坊 · API接口采集与数据网关平台

面向第三方API接入注册、接口鉴权编排、限流熔断、响应解析、schema映射、结果缓存、重放恢复与交付订阅的API接口采集与数据网关平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 接口提供方 | `api_provider` | API提供方 | 提供方编号、提供方名称、认证方式 |
| 鉴权配置 | `auth_profile` | 鉴权方案 | 方案编号、提供方、鉴权类型 |
| 接口定义 | `endpoint_def` | 接口端点 | 端点编号、提供方、请求路径 |
| 限流熔断 | `rate_limit` | 限流策略 | 策略编号、目标端点、QPS上限 |
| 请求批次 | `request_batch` | 调用批次 | 批次编号、关联端点、参数模板 |
| 响应解析 | `response_parse` | 解析规则 | 规则编号、目标端点、JSON路径 |
| Schema差异 | `schema_diff` | 差异记录 | 记录编号、关联端点、变更字段 |
| 结果缓存 | `data_cache` | 缓存策略 | 策略编号、目标端点、缓存TTL |
| 重放队列 | `retry_queue` | 重放任务 | 任务编号、失败端点、失败原因 |
| 数据投递 | `data_delivery` | 投递管道 | 管道编号、数据来源、目标地址 |
| 订阅管理 | `subscriber` | 订阅方 | 订阅编号、订阅方、数据主题 |
| 接口监控 | `api_monitor` | 监控指标 | 指标编号、目标端点、监控类型 |
| Webhook路由 | `webhook_router` | 回调规则 | 规则编号、触发事件、目标URL |
| 传输加密 | `data_encrypt` | 加密配置 | 配置编号、数据范围、加密算法 |
| 调用日志 | `call_log` | 调用记录 | 记录编号、接口端点、请求参数 |
| 数据版本 | `data_version` | 数据快照 | 版本编号、数据来源、行数统计 |
| 管道审计 | `pipeline_audit` | 审计记录 | 记录编号、管道名称、操作人 |
| 治理报告 | `govern_report` | 治理报告 | 报告编号、评估范围、合规指标 |
| 增量追踪 | `incr_tracker` | 增量标记 | 标记编号、目标端点、增量字段 |
| 抽样校验 | `sample_check` | 抽样计划 | 计划编号、目标端点、抽样比例 |
| 用量计费 | `cost_account` | 用量账单 | 账单编号、提供方、计费周期 |
| 任务调度 | `task_schedule` | 调度计划 | 计划编号、关联端点、Cron表达式 |
| 采集处理 | `collection` | 补采申请/分时补采批次 | 申请编号、关联端点、时段、覆盖申请、补采结论 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 采集处理（分时补采）

停运时段的补采不再逐个建请求批次，而是由值班员一次提交多条申请，
`app/services/collection.py` 按端点实际覆盖切成互不重叠的分时补采批次：

- 同一端点可提交多条部分重叠的申请（如 08:00-10:00 与 09:00-11:00），
  切分结果为 08:00-09:00 / 09:00-10:00 / 10:00-11:00，重叠段合并到同一批次；
- 成功回执只把**尚未入账**的部分写入覆盖账本 `collection_coverage`，
  同一段成功数据只计一次，重叠申请各自回看自己的实际补采时段；
- 每个分时批次同步一条请求批次（`request_batch`）的 `补采清单`，
  逐段回写状态、回执编号和已覆盖/未覆盖时段；
- 中断/部分成功的剩余区间自动进入 `retry_queue`（队列状态 `补采待重放`），
  `POST /api/collection/resume` 先核对已提交的成功回执再派发未覆盖片段；
- 批量重复提交按申请时段复用申请与分时批次，不会重复入账。

主要接口（均挂在 `/api/collection` 下）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/submit` | 一次提交多条申请，返回切分片段与请求批次 |
| GET | `/applications` | 申请清单与各申请实际补采时段 |
| GET | `/batches` | 分时批次、补采结论、回执明细 |
| POST | `/batches/{id}/receipt` | 登记单段回执（成功/中断/失败） |
| POST | `/batches/{id}/dispatch` | 派发（自动跳过已覆盖区间） |
| POST | `/resume` | 核对回执后派发恢复片段 |
| GET | `/report` | 补采结果、各申请回执、恢复后应执行片段 |
| GET | `/gaps` | 查询端点窗口内尚未覆盖的缺采时段 |

前端入口：侧边栏「采集处理」（`frontend/src/views/CollectionView.vue`）。

回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`。

领域：数据工程
子方向：数据采集、爬虫与RPA
