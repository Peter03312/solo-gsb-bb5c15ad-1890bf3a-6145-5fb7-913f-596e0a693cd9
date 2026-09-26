# 博物馆展柜照度预算 API

基于 **FastAPI + SQLite** 的博物馆展柜照明曝光预算服务：客户端登记展柜与展品（敏感度、年度
lux·hour 上限），提交带起止时间的照明区间；服务在时间线上合并同一展柜的重叠灯光（**重叠时
取最大照度**），累计每件展品的实际曝光量，并报告**首次超预算时刻、超出量与每日剩余额度**。

## 一键启动

```bash
docker compose up --build
```

- API 服务：<http://localhost:8000>
- 交互式文档（Swagger）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- SQLite 文件持久化在 Docker 卷 `museum-data`（容器内 `/data/museum.db`），容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
DB_PATH=./data/museum.db uvicorn app.main:app --reload
```

启动时会**自动建库建表**（幂等），无需手动迁移。

## 快速上手

```bash
# 1. 建展柜
curl -s -X POST localhost:8000/api/v1/showcases \
  -H 'Content-Type: application/json' \
  -d '{"name":"敦煌展厅A柜","location":"二层"}'

# 2. 登记展品：敏感度 + 自定义年度上限（lux·hour）
curl -s -X POST localhost:8000/api/v1/showcases/1/exhibits \
  -H 'Content-Type: application/json' \
  -d '{"name":"唐代绢画","sensitivity":"high","annual_limit_lux_hours":500}'

# 3. 批量提交照明区间（ISO 8601；不带时区视为 UTC）
curl -s -X POST localhost:8000/api/v1/showcases/1/intervals \
  -H 'Content-Type: application/json' \
  -d '{"intervals":[
    {"start":"2026-09-18T08:00:00Z","end":"2026-09-18T16:00:00Z","lux":50},
    {"start":"2026-09-18T12:00:00Z","end":"2026-09-18T13:00:00Z","lux":300},
    {"start":"2026-09-19T08:00:00Z","end":"2026-09-19T12:00:00Z","lux":50}
  ]}'

# 4. 按展柜 + 日期查询预算
curl -s "localhost:8000/api/v1/showcases/1/budget?day=2026-09-18"
```

照明调整试算（不落库）：假设撤去区间 2（300 lux 重点照明）、9-19 改开 100 lux，
对比每件展品的 现状 / 试算 年度曝光与首次越限时刻：

```bash
curl -s -X POST localhost:8000/api/v1/showcases/1/lighting-preview \
  -H 'Content-Type: application/json' \
  -d '{
    "day": "2026-09-18",
    "remove_interval_ids": [2],
    "add_intervals": [
      {"start": "2026-09-19T08:00:00Z", "end": "2026-09-19T16:00:00Z", "lux": 100}
    ]
  }'
```

响应示例（现状 850 lux·h；试算 1200 lux·h，差额 +350，越限时刻从 9-18 13:00 推迟到 9-19 09:00）：

```json
{
  "showcase_id": 1,
  "showcase_name": "敦煌展厅A柜",
  "query_date": "2026-09-18T00:00:00Z",
  "year": 2026,
  "removed_interval_ids": [2],
  "added_interval_count": 1,
  "exhibits": [{
    "exhibit_id": 1,
    "exhibit_name": "唐代绢画",
    "sensitivity": "high",
    "annual_limit_lux_hours": 500.0,
    "year": 2026,
    "current_total_lux_hours": 850.0,
    "preview_total_lux_hours": 1200.0,
    "delta_lux_hours": 350.0,
    "current_first_exceed_at": "2026-09-18T13:00:00Z",
    "preview_first_exceed_at": "2026-09-19T09:00:00Z"
  }]
}
```

响应示例（重叠段 12:00–13:00 取 max(50,300)=300 lux；首日累计 650 lux·h，13:00 首次超标）：

```json
{
  "showcase_id": 1,
  "showcase_name": "敦煌展厅A柜",
  "query_date": "2026-09-18T00:00:00Z",
  "exhibits": [{
    "exhibit_id": 1,
    "exhibit_name": "唐代绢画",
    "sensitivity": "high",
    "annual_limit_lux_hours": 500.0,
    "year": 2026,
    "total_lux_hours": 850.0,
    "remaining_lux_hours": 0.0,
    "first_exceed_at": "2026-09-18T13:00:00Z",
    "exceeded_lux_hours": 350.0,
    "daily_quota_lux_hours": 1.369863,
    "daily_used_lux_hours": 650.0,
    "daily_remaining_lux_hours": -648.630137,
    "annual_remaining_at_date": -150.0
  }]
}
```

安全照度上限试算（不落库）：计划 9-20 08:00–16:00 追加一段照明，求该时段内可使用的
**最大整数照度**。以该年既有区间合并（重叠取 max）的时间线为基线，候选区间按不同照度
重新合并并逐件核算全年曝光，仅当所有有限额展品都不越过各自年度上限时可行：

```bash
curl -s -X POST localhost:8000/api/v1/showcases/1/safe-lux-limit \
  -H 'Content-Type: application/json' \
  -d '{"start": "2026-09-20T08:00:00Z", "end": "2026-09-20T16:00:00Z"}'
```

展柜 1 现状年度曝光 850 lux·h 已超过绢画上限 500，返回不可行与超限展品（不给出安全上限）：

```json
{
  "showcase_id": 1,
  "showcase_name": "敦煌展厅A柜",
  "year": 2026,
  "start": "2026-09-20T08:00:00Z",
  "end": "2026-09-20T16:00:00Z",
  "feasible": false,
  "max_safe_lux": null,
  "binding_exhibits": [],
  "exceeded_exhibits": [{
    "exhibit_id": 1,
    "exhibit_name": "唐代绢画",
    "annual_limit_lux_hours": 500.0,
    "remaining_lux_hours": -350.0
  }]
}
```

换一个未超限的场景（年度上限 1200、现状 600 lux·h、计划时段 8h 无既有照明）：
`600 + 8L ≤ 1200` → 最大整数照度 75 lux，瓶颈展品剩余额度恰好为 0：

```json
{
  "showcase_id": 2,
  "showcase_name": "书画展厅B柜",
  "year": 2026,
  "start": "2026-09-20T08:00:00Z",
  "end": "2026-09-20T16:00:00Z",
  "feasible": true,
  "max_safe_lux": 75,
  "binding_exhibits": [{
    "exhibit_id": 2,
    "exhibit_name": "宋代山水卷",
    "annual_limit_lux_hours": 1200.0,
    "remaining_lux_hours": 0.0
  }],
  "exceeded_exhibits": []
}
```

## 计算规则

- **时间线合并**：把展柜所有区间的开/关灯时刻作为事件扫描；任意时刻有多盏灯亮，有效照度取
  **最大值**（`max`，不累加）；相邻同照度片段自动合并。
- **曝光量**：`曝光(lux·hour) = 有效照度(lux) × 时长(hour)`，按公历年（UTC 1/1 起）累计；
  跨年区间自动裁剪到当年。
- **首次超预算时刻**：片段内照度恒定、曝光线性增长，越界时刻按
  `片段起点 + (上限 − 已累计) / lux` 精确插值；**恰好等于上限不算超标**。
- **每日剩余额度**：
  - `daily_quota_lux_hours = 年度上限 / 当年天数`（闰年 366 天）
  - `daily_remaining_lux_hours = 日额度 − 查询日当天已用`（负值表示当日已超日额度）
  - 另有 `annual_remaining_at_date`：截至查询日 24:00 的年度剩余额度（可为负）。
- **不限量展品**：敏感度 `low` 且不提供自定义上限时，年度上限为 `null`，所有"剩余/超标"字段
  返回 `null`/`0`，仍正常统计累计曝光。
- **照明调整试算**：`POST .../lighting-preview` 在查询日所属 UTC 公历年内，对 现状区间 与
  "撤去 `remove_interval_ids` + 新增 `add_intervals`" 后的区间分别执行同一套合并与曝光积分，
  按展品返回两侧年度曝光、差额与首次越限时刻；全程只读，不改动已存区间。
- **安全照度上限**：`POST .../safe-lux-limit` 传入计划照明起止时间（UTC 归一化后须起点早于
  终点且处于同一公历年），以该年既有区间合并（重叠取 max）的时间线为基线，把候选区间按照度
  L 重新合并并积分全年曝光；曝光关于 L 单调不减，故二分求 0～100000 间的最大整数照度，使
  全部有限额展品年度曝光都不越过各自上限（恰好到限不算超标）。返回 `max_safe_lux` 与该上限
  处年度剩余额度最少的瓶颈展品 `binding_exhibits`（共享同一时间线，瓶颈即上限最小者，可并列）；
  无有限额展品时返回 100000 lux 与空列表；现状已超限则 `feasible=false`、`max_safe_lux=null`
  并在 `exceeded_exhibits` 列出超限展品（剩余额度为负）。全程只读，不改动已存区间。
- **时间**：接受带时区 ISO 8601；naive 时间视为 UTC；内部统一 UTC 存储与返回。

### 敏感度默认年度上限

| 敏感度 | 典型展品 | 默认年度上限 (lux·hour) |
|---|---|---|
| `high` | 丝织品、纸本、水彩 | 15,000 |
| `medium` | 油画、木器、骨角器 | 150,000 |
| `low` | 石材、金属、陶瓷 | 不限量（`null`） |

登记展品时传 `annual_limit_lux_hours` 可覆盖默认值（`0` 表示显式设为 0，非空即取自定义值）。

## 输入校验（全部返回 422 / 404）

- **负照度**：`lux < 0` → `422`（照度上限 100,000 lux）。
- **反向 / 零长度区间**：`end <= start` → `422`。
- **安全照度上限时段**：UTC 归一化后 `end <= start`，或起止时间不处于同一公历年 → `422`。
- **未知展品 / 未知展柜**：查询或向不存在的展柜提交数据 → `404`。
- **试算撤去项**：`remove_interval_ids` 含不存在或不属于该展柜的区间 ID → `404`。
- 非有限数值（NaN / Infinity）→ `422`；重名冲突 → `409`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（实际执行 SQLite 查询） |
| GET | `/docs` | Swagger 交互文档 |
| POST | `/api/v1/showcases` | 新建展柜 |
| GET | `/api/v1/showcases` | 展柜列表 |
| GET | `/api/v1/showcases/{id}` | 展柜详情（不存在 → 404） |
| POST | `/api/v1/showcases/{id}/exhibits` | 登记展品（敏感度 + 年度上限） |
| GET | `/api/v1/showcases/{id}/exhibits` | 展柜内展品 |
| GET | `/api/v1/exhibits/{id}` | 展品详情（未知 → 404） |
| PATCH | `/api/v1/exhibits/{id}` | 展品移柜（目标柜未知 → 404） |
| POST | `/api/v1/showcases/{id}/intervals` | **批量提交照明区间** |
| POST | `/api/v1/showcases/{id}/lighting-preview` | **照明调整试算**（撤去/新增区间，不落库） |
| POST | `/api/v1/showcases/{id}/safe-lux-limit` | **安全照度上限**（计划时段可追加的最大整数照度，不落库） |
| GET | `/api/v1/showcases/{id}/intervals?day=YYYY-MM-DD` | 区间查询，**可按日期过滤** |
| GET | `/api/v1/showcases/{id}/budget?day=YYYY-MM-DD` | **按展柜 + 日期查预算** |
| GET | `/api/v1/exhibits/{id}/budget?day=YYYY-MM-DD` | 按单件展品查预算 |
| GET | `/api/v1/budgets?showcase_id=&day=` | 跨展柜批量预算（不带 id 返回全部） |

`day` 参数省略时默认今天（UTC）。

## 项目结构

```
app/
├── main.py            # FastAPI 入口：lifespan 建库、请求级事务、健康检查
├── config.py          # 环境变量配置（DB_PATH 等）
├── database.py        # SQLite 连接 / 建表 / 事务
├── schemas.py         # Pydantic 模型与输入校验
├── timeline.py        # 扫描线合并、积分、首次超标插值（纯算法）
├── budget.py          # 报表组装服务
└── routers/
    ├── showcases.py   # 展柜 / 展品 / 照明区间
    └── reports.py     # 预算查询
tests/                 # pytest：算法单测 + API 集成测试（35 项）
Dockerfile             # python:3.12-slim + HEALTHCHECK
docker-compose.yml     # 单命令启动 + 数据卷
```

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

覆盖：重叠取 max、多灯合并、首尾相接、跨年裁剪、超标插值、恰好到限不超标、负照度/反向区间/
未知展柜拒绝、按日期过滤、安全照度上限（二分最大整数照度、瓶颈展品、现状超限不可行、跨年
时段拒绝、只读性）、重启持久化等。
