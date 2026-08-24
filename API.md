# 命理解读师 · 多步骤 API 文档

本地 HTTP 服务，MySQL 存储会话状态。**第一步排盘返回 id，后续步骤都带着这个 id 往下走。**

- 服务地址：http://127.0.0.1:8000
- 可视化调用面板（网页）：http://127.0.0.1:8000/
- 在线接口文档（Swagger，可点着调用）：http://127.0.0.1:8000/docs
- 接口列表 JSON：http://127.0.0.1:8000/api

---

## 1. 启动服务

~~~bash
# Windows PowerShell 设置密钥
$env:DEEPSEEK_API_KEY = "sk-你的DeepSeek密钥"

# 手相照片识别用通义千问 qwen-vl，需要 DashScope 密钥：
$env:DASHSCOPE_API_KEY = "sk-你的DashScope密钥"

# 启动（端口 8000 若被占用，改成 --port 8010）
py -m uvicorn mingli_server:app --host 127.0.0.1 --port 8000
~~~

> 依赖（首次需安装）：py -m pip install iztro-py fastapi "uvicorn[standard]" pymysql python-multipart

---

## 2. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DEEPSEEK_API_KEY | 空 | DeepSeek 密钥（排盘后解读/交叉比对用） |
| DEEPSEEK_MODEL | deepseek-v4-pro | 可选 deepseek-v4-flash |
| DASHSCOPE_API_KEY | 空 | 通义千问 qwen-vl 密钥（手相照片识别用） |
| QWEN_VL_MODEL | qwen-vl-max | 视觉模型 |
| DB_HOST / DB_PORT | 127.0.0.1 / 3306 | MySQL |
| DB_USER / DB_PASSWORD | root / 空 | MySQL 账号（请用环境变量配置密码） |
| DB_NAME | mingli | 数据库名 |

---

## 3. 接口总览

| 步骤 | 方法 | 路径 | 说明 |
|------|------|------|------|
| ① | POST | /api/chart | 排盘（公历/农历），返回 id |
| ② | GET | /api/{id}/questions | 出题（校准问题，可选） |
| ② | POST | /api/{id}/answers | 提交答案（可选） |
| ③ | POST | /api/{id}/palm | 上传手相照片/文字描述 → 特征提取 + 交叉比对（可选） |
| ④ | POST | /api/{id}/interpret | 大模型解读 → 生成 reading + HTML |
| - | GET | /api/{id} | 查看会话状态 |
| - | GET | /api/{id}/chart | 查看完整排盘 JSON |
| - | GET | /api/{id}/html | 下载命盘 HTML |
| 辅助 | GET | /api/hours | 时辰索引对照表（可 ?time=HH:MM 反查） |
| 小程序 | POST | /api/miniapp/login | 微信小程序登录/生成用户标识 |
| 小程序 | POST | /api/miniapp/chart | 小程序排盘并绑定当前用户 |
| 小程序 | GET | /api/miniapp/history | 当前用户解读历史记录 |
| 小程序 | GET | /api/miniapp/{token}/chart | 查看当前用户命盘 |
| 小程序 | GET | /api/miniapp/{token}/record | 回看当前用户某次完整解读 |
| 小程序 | GET | /api/miniapp/{token}/questions | 小程序出题 |
| 小程序 | POST | /api/miniapp/{token}/answers | 小程序提交答案 |
| 小程序 | POST | /api/miniapp/{token}/interpret | 小程序解读，返回命盘与 reading |
| 小程序八字 | POST | /api/miniapp/bazi/chart | 小程序八字排盘并绑定当前用户 |
| 小程序八字 | GET | /api/miniapp/bazi/history | 当前用户八字分析历史 |
| 小程序八字 | GET | /api/miniapp/bazi/{token}/record | 回看当前用户某次八字分析 |
| 小程序八字 | POST | /api/miniapp/bazi/{token}/interpret | 小程序八字综合分析 |
| 八字① | POST | /api/bazi/chart | 八字排盘（四柱/大运/流年/神煞），返回 id |
| 八字② | POST | /api/bazi/{id}/interpret | DeepSeek 八字解读 → 报告 HTML |
| 八字- | GET | /api/bazi/{id} | 八字会话状态 |
| 八字- | GET | /api/bazi/{id}/html | 八字报告 HTML |

---

## 4. 最快上手：完整流程（curl.exe）

~~~bash
# ① 排盘（公历）—— 记住返回的 id
curl.exe -X POST http://127.0.0.1:8000/api/chart -H "Content-Type: application/json" -d '{"calendar":"solar","date":"1991-8-15","hour":1,"gender":"男","year":2026}'

# 假设返回 {"id": 5, ...}，下面都用 5

# ② 出题
curl.exe http://127.0.0.1:8000/api/5/questions

# ② 提交答案
curl.exe -X POST http://127.0.0.1:8000/api/5/answers -H "Content-Type: application/json" -d '{"answers":[{"id":"q1_marriage","answer":"已婚稳定"},{"id":"q3_finance","answer":"收入稳定"}]}'

# ③ 手相：二选一（照片识别 或 文字描述）
curl.exe -X POST http://127.0.0.1:8000/api/5/palm -F "image=@掌纹.jpg"
curl.exe -X POST http://127.0.0.1:8000/api/5/palm -F "description=生命线长而清晰，智慧线下弯，感情线深"

# ④ 大模型解读
curl.exe -X POST http://127.0.0.1:8000/api/5/interpret

# 浏览器打开命盘 HTML
start http://127.0.0.1:8000/api/5/html
~~~

---

## 5. 各接口详情

### 时辰索引  GET /api/hours

返回完整时辰索引对照表；带 ?time=HH:MM 可反查某时刻对应的时辰。

~~~json
{
  "hours": [
    { "index": 0, "name": "早子时", "time": "23:00-00:00" },
    { "index": 1, "name": "丑时", "time": "01:00-03:00" },
    { "index": 6, "name": "午时", "time": "11:00-13:00" },
    { "index": 11, "name": "亥时", "time": "21:00-23:00" },
    { "index": 12, "name": "晚子时", "time": "23:00-00:00" }
  ],
  "usage": "POST /api/chart 的 hour 参数填 index；也可 GET /api/hours?time=02:30 反查"
}
~~~

反查示例：

~~~bash
curl.exe "http://127.0.0.1:8000/api/hours?time=02:30"
# -> {"time":"02:30","index":1,"name":"丑时","time_range":"01:00-03:00"}
~~~

### ① 排盘  POST /api/chart

请求体（JSON）：

~~~json
{
  "calendar": "solar",     // solar=公历  lunar=农历
  "date": "1991-8-15",     // YYYY-M-D
  "hour": 1,               // 时辰索引 0-12，见下表
  "gender": "男",           // 男 | 女
  "leap": false,           // 农历闰月，仅 lunar 时有效
  "year": 2026             // 目标流年，可省略（默认当前年）
}
~~~

农历示例：

~~~json
{ "calendar": "lunar", "date": "1991-7-6", "hour": 1, "gender": "男", "leap": false, "year": 2027 }
~~~

**时辰索引对照：**

| 索引 | 时辰 | 时间 |
|------|------|------|
| 0 | 早子时 | 23:00-00:00 |
| 1 | 丑时 | 01:00-03:00 |
| 2 | 寅时 | 03:00-05:00 |
| 3 | 卯时 | 05:00-07:00 |
| 4 | 辰时 | 07:00-09:00 |
| 5 | 巳时 | 09:00-11:00 |
| 6 | 午时 | 11:00-13:00 |
| 7 | 未时 | 13:00-15:00 |
| 8 | 申时 | 15:00-17:00 |
| 9 | 酉时 | 17:00-19:00 |
| 10 | 戌时 | 19:00-21:00 |
| 11 | 亥时 | 21:00-23:00 |
| 12 | 晚子时 | 23:00-00:00 |

响应（关键字段）：

~~~json
{
  "id": 5,
  "status": "created",
  "basic": { "solar": "1991-8-15", "lunar": "一九九一年七月初六", "five_elements": "金四局", "soul_palace_branch": "未", "body_palace_branch": "酉" },
  "gezhi": { "gezhi_name": "紫府武相格", "quant_metrics": { "risk_tolerance": 2, "fluctuation_resilience": 5, "decision_aggressiveness": 2 } },
  "summary": "【基本信息】...（命盘中文摘要）",
  "next": [ "GET /api/5/questions", "POST /api/5/palm", "POST /api/5/interpret" ]
}
~~~

### ② 出题  GET /api/{id}/questions

~~~json
{
  "id": 5,
  "questions": [
    { "id": "q1_marriage", "text": "你的感情/婚姻状态属于哪种？", "hint": "（可简单描述：稳定/波折/单身/其他）", "priority": "high" },
    { "id": "q3_finance", "text": "最近1-2年财务状况如何？", "hint": "（收入变化、投资损益、负债等）", "priority": "high" }
  ],
  "next": "POST /api/5/answers"
}
~~~

### ② 提交答案  POST /api/{id}/answers

请求体（两种格式都行）：

~~~json
{ "answers": [ { "id": "q1_marriage", "answer": "已婚稳定" }, { "id": "q3_finance", "answer": "收入稳定" } ] }
~~~

或简写：

~~~json
{ "answers": { "q1_marriage": "已婚稳定", "q3_finance": "收入稳定" } }
~~~

响应：

~~~json
{ "id": 5, "received": 2, "next": ["POST /api/5/palm", "POST /api/5/interpret"] }
~~~

### ③ 手相  POST /api/{id}/palm（multipart/form-data）

两种方式，任选其一：

- 照片：image=@掌纹.jpg（通义千问 qwen-vl 识别掌纹）
- 描述：description=生命线长而清晰（直接文字描述）

响应：

~~~json
{
  "id": 5,
  "palm_features": "{ \"life_line\": \"生命线：...\", \"head_line\": \"...\", \"heart_line\": \"...\" }",
  "hand_reading": {
    "items": [
      { "title": "生命线", "body": "...", "status": "match", "status_text": "与疾厄宫太阳化权共振 ✓" },
      { "title": "智慧线", "body": "...", "status": "conflict", "status_text": "与命宫武曲星性有表面矛盾", "resolution": "取舍逻辑..." }
    ]
  },
  "next": "POST /api/5/interpret"
}
~~~

### ④ 大模型解读  POST /api/{id}/interpret

无需请求体。调用 deepseek-v4-pro 生成解读（约 60-90 秒）。

响应：

~~~json
{
  "id": 5,
  "reading": { "cards": [ "...7张卡片..." ], "hand_reading": { "items": [ "...手相互证..." ] }, "calibration_questions": [] },
  "html_url": "/api/5/html"
}
~~~

### 微信小程序接口

小程序目录在 `miniprogram/`，默认请求本地服务 `http://127.0.0.1:8000`。开发者工具本地调试时可关闭域名校验；真机/上线时请把 `miniprogram/app.js` 里的 `apiBase` 改成已备案且配置到微信后台的 HTTPS 域名。

生产环境建议配置：

| 变量 | 说明 |
|------|------|
| WECHAT_APPID | 小程序 AppID |
| WECHAT_SECRET | 小程序 AppSecret |
| USER_KEY_SALT | openid 派生 user_key 的盐，可自定义 |
| MINIAPP_DAILY_INTERPRET_LIMIT | 小程序用户每日解读次数，默认 1 |
| REQUIRE_HTTPS | 生产环境设为 1 后，小程序接口拒绝 HTTP |

#### 登录/绑定  POST /api/miniapp/login

请求体：

~~~json
{ "code": "wx.login 返回的 code" }
~~~

响应：

~~~json
{ "user_key": "wx_xxx", "has_openid": true, "debug_guest": false }
~~~

未配置 `WECHAT_APPID / WECHAT_SECRET` 的本地调试场景，也可以不传 `code`，服务端会返回 `guest_xxx` 临时用户标识。后续小程序接口都要带请求头：

~~~text
X-Mingli-User: wx_xxx
~~~

#### 小程序排盘  POST /api/miniapp/chart

请求体同 `POST /api/chart`，响应会额外返回适合小程序直接渲染的 `palaces`、`metrics`、`time_travel`：

~~~json
{
  "calendar": "solar",
  "date": "1991-8-15",
  "hour": 1,
  "gender": "男",
  "leap": false,
  "year": 2026
}
~~~

小程序接口不会向客户端暴露数据库自增编号，排盘后返回随机 `token`，后续请求都使用 `token`。上线防抓包请使用 HTTPS 域名；可设置 `REQUIRE_HTTPS=1` 强制小程序接口拒绝 HTTP；不要把 DeepSeek、微信 AppSecret 或任何服务端密钥放进小程序前端。

#### 小程序补充与解读

~~~bash
GET  /api/miniapp/{token}/questions
POST /api/miniapp/{token}/answers
POST /api/miniapp/{token}/interpret
~~~

服务器算力有限，小程序接口默认限制同一用户每天只生成 1 次新解读。再次查看已经生成过的旧解读不消耗次数；如果当天已达上限，`POST /api/miniapp/{token}/interpret` 返回 429。

`POST /api/miniapp/{token}/interpret` 返回：

~~~json
{
  "token": "mp_xxx",
  "chart": { "palaces": [], "summary": "..." },
  "reading": { "cards": [] },
  "html_url": "/api/miniapp/mp_xxx/html"
}
~~~

#### 小程序历史记录

~~~bash
GET /api/miniapp/history
GET /api/miniapp/{token}/record
~~~

`GET /api/miniapp/history` 返回当前用户最近 50 条已解读记录，以及当天使用次数：

~~~json
{
  "today_usage": { "used": 1, "limit": 1, "remaining": 0 },
  "records": [
    {
      "token": "mp_xxx",
      "solar": "1991-8-15",
      "lunar": "一九九一年七月初六",
      "target_year": 2026,
      "gezhi_name": "紫府武相格",
      "preview": "核心判断摘要...",
      "interpreted_at": "2026-08-18 14:30:00"
    }
  ]
}
~~~

`GET /api/miniapp/{token}/record` 返回该条历史的完整 `chart` 和 `reading`，用于小程序回溯展示。

#### 小程序八字接口

入口页点击“定”进入八字定盘。小程序八字接口同样不暴露数据库自增编号，排盘后返回随机 `token`。

~~~bash
POST /api/miniapp/bazi/chart
POST /api/miniapp/bazi/{token}/interpret
GET  /api/miniapp/bazi/history
GET  /api/miniapp/bazi/{token}/record
~~~

排盘请求体：

~~~json
{
  "calendar": "solar",
  "date": "1991-8-15",
  "sex": "男",
  "shichen": "午",
  "leap": false
}
~~~

排盘响应返回 `report`，内容包含年柱、月柱、日柱、时柱、大运与流年等文本。综合分析调用 `POST /api/miniapp/bazi/{token}/interpret`，返回：

~~~json
{
  "token": "mp_xxx",
  "report": "四柱八字排盘文本...",
  "reading": {
    "summary": "命局总评",
    "day_master": "日主强弱分析",
    "wuxing": "五行平衡",
    "shishen": "十神关系",
    "geju": "格局判定",
    "dayun": "大运流年",
    "liunian": "流年",
    "aspects": {
      "career": "事业建议",
      "relationship": "感情建议",
      "health": "健康建议"
    }
  }
}
~~~

八字综合分析与紫微解读共用小程序每日解读额度。

### 查看状态  GET /api/{id}

~~~json
{
  "id": 5,
  "status": "interpreted",
  "basic": { "solar": "1991-8-15", "lunar": "一九九一年七月初六", "hour_index": 1, "gender": "男", "target_year": 2026 },
  "gezhi_name": "紫府武相格",
  "has_chart": true,
  "has_answers": true,
  "has_palm": true,
  "has_reading": true,
  "html_url": "/api/5/html"
}
~~~

---

## 5.5 八字（BaZi）接口

### ① 排盘  POST /api/bazi/chart

~~~json
{ "calendar": "solar", "date": "1990-5-15", "sex": "男", "hour": "12:30" }
~~~

农历 + 时辰地支（hour 与 shichen 二选一）：

~~~json
{ "calendar": "lunar", "date": "1990-4-21", "sex": "女", "shichen": "午", "leap": false }
~~~

| 字段 | 说明 |
|------|------|
| calendar | solar 公历 / lunar 农历 |
| date | YYYY-M-D |
| sex | 男 / 女（必填） |
| hour | 出生钟点 HH:MM（北京时间），与 shichen 二选一 |
| shichen | 时辰地支：子丑寅卯辰巳午未申酉戌亥 |
| leap | 农历闰月（仅 lunar） |
| place | 出生地（可选，仅展示/警示） |
| deceased_year | 已故年份（可选，流年只列到该年） |

响应返回 id + report（四柱/大运/流年/神煞文本）。

### ② 解读  POST /api/bazi/{id}/interpret

无需请求体，约 1-2 分钟。返回 reading（日主强弱/五行喜忌/十神/格局/大运/分项）+ html_url。

### 查看  GET /api/bazi/{id}  和  GET /api/bazi/{id}/html

~~~bash
curl.exe http://127.0.0.1:8000/api/bazi/1          # 状态
curl.exe http://127.0.0.1:8000/api/bazi/1/html     # 报告 HTML
~~~

---

## 6. 状态流转

created → answered（提交答案后）→ palm（手相比对后）→ interpreted（解读完成）

每步都可跳过：排盘后直接调 interpret 也能出命盘（跳过校准和手相）。

---

## 7. 产物与存储

- 命盘 HTML 存于 output/session_{id}/mingpan.html，浏览器直接打开可看，右上角切换「典藏/赛博」双主题。
- 手相照片存于 uploads/ 目录。
- 会话全量状态存于 MySQL 库 mingli 的 sessions 表（chart_json / answers_json / palm_features / hand_reading_json / reading_json / html_path）。

---

## 8. 常见错误

| 状态码 | 说明 |
|--------|------|
| 400 | 参数错误（calendar/性别/时辰越界、日期格式错） |
| 404 | 会话 id 不存在，或还没生成 HTML |
| 500 | 未配置 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY，或大模型调用失败 |
