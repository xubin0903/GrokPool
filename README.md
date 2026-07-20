# GrokPool

**把免费 Grok 号变成“能打的号池”，不是一堆注册完就躺平的 cookie。**

```text
批量注册  →  正确 OAuth（grok-build）  →  智能调度  →  OpenAI 网关  →  CC Switch / Claude Code
```

面向真实痛点：**账号很多、单号额度很小（约 1M token/窗）、导入易 403、调度爱排队、死号难清。**  
GrokPool 把注册机 + 改造版 Sub2API 收成一条可维护流水线。

| 你可能正在受的罪 | GrokPool 怎么解 |
|---|---|
| 导进去全是 `permission-denied` | 强制授权码 + `referrer=grok-build`，缺 claim 不入库 |
| 明明有空闲号还在排队 | 免费号 TopK 溢出 / free-slot / sticky 逃逸 |
| 残血号接大请求直接爆 | 剩余 token 感知调度 |
| 死号混在池子里毒打 | 批量死号探测，真封禁才标死 |
| 每个客户端都要自己对接 xAI | 统一 OpenAI `/v1`，CC Switch 开箱可配 |
| 本地改了代码容器还是旧的 | `build` + `check-parity` 保证源码=镜像=运行中 |

> 仅供学习研究与自建测试。自动化注册/使用可能违反平台条款，后果自负。

---

## 为什么选 GrokPool（项目特色）

### 注册只是起点，进池才算交付
很多工具停在“注册成功”。GrokPool 默认继续做完：
**SSO → 授权码 OAuth → Sub2 `type=oauth` 入库 → 分组绑定**。  
日志一眼能看懂：`[CPA] OK` 之后必须有 `[SUB2] PUSH OK`。

### 踩过的坑已经焊死在流程里
免费 Grok 最坑的不是验证码，是 **换票方式**：
- 裸 SSO 不顶用  
- Device Flow 经常没有 `grok-build` claim  
- base_url 走错变成计费通道  

本项目把正确路径做成默认，不允许“看起来导入成功、实际全员不能聊”。

### 专治「多小号」调度
不是按大额度会员号那套逻辑硬套。针对 **海量 1M 窗免费号** 做了剩余额度感知、空号少排队、429 更克制、死号不参与调度。

### 给最终用户的接口足够标准
对上只暴露 **OpenAI 兼容网关**：
`base_url + api_key + model`。  
CC Switch / Claude Code / 自建 Bot 都按这套接，不用理解 xAI 内部 cookie。

### 可运营，不是一次性脚本
- 管理端批量探测死号  
- 软失败（billing/CF/429）不误删  
- 代码与 Docker 可校验一致  
- Windows 实战文档：TUN 代理、端口、分组、Token、排错  

### 一句话卖点
**GrokPool = 免费 Grok 号的“生产 + 仓储 + 调度 + 出货”整厂方案。**

---

## 目录

- [项目简介](#项目简介)
- [为什么选 GrokPool（项目特色）](#为什么选-grokpool项目特色)
- [核心优势](#核心优势)
- [和其他方案比](#和其他方案比)
- [架构](#架构)
- [功能一览](#功能一览)
- [环境要求](#环境要求)
- [快速开始（Windows）](#快速开始windows)
- [Sub2API Docker 详细教程](#sub2api-docker-详细教程)
- [代理：必须开穿透 / 虚拟网卡](#代理必须开穿透--虚拟网卡)
- [注册机面板](#注册机面板)
- [OAuth 硬规则（必读）](#oauth-硬规则必读)
- [Cloudflare 自建临时邮箱（推荐）](#cloudflare-自建临时邮箱推荐)
- [死号探测](#死号探测)
- [CC Switch / Claude Code（OpenAI 格式）](#cc-switch--claude-codeopenai-格式)
- [代码与 Docker 一致性](#代码与-docker-一致性)
- [目录结构](#目录结构)
- [配置说明](#配置说明)
- [常见问题](#常见问题)
- [上传 GitHub 注意](#上传-github-注意)
- [许可证](#许可证)

---

## 项目简介

GrokPool 解决的是这条链路上最容易翻车的几件事：

1. **免费 Grok 号怎么稳定注册**（浏览器自动化 + 激活完整性）
2. **注册完怎么变成能 chat 的凭证**（不是裸 SSO，而是带 `referrer=grok-build` 的 OAuth）
3. **几百个小额度号怎么调度**（剩余 token 感知，避免空号/残血号接大请求）
4. **怎么给 Claude Code / CC Switch 用**（标准 OpenAI `/v1` 网关）
5. **死号怎么清**（批量探测真封禁，不把 billing 抖动误杀）

它不是“又一个注册脚本”，而是：

```text
注册机（Windows 宿主机）
   + 授权码 OAuth 转换
   + 自定义 Sub2API 镜像（调度 / 死号 / OAuth）
   + 本地 Docker 一键栈
   + 客户端 OpenAI 接入文档
```

适合：

- 自己养免费 Grok OAuth 池，给 CC Switch / 自建网关用  
- 已经在用 Sub2API，但免费号调度差、导入后秒 403、死号难清理  
- 想要 **代码与 Docker 镜像可校验一致** 的可复现部署  

不适合：

- 期望一个号无限额度  
- 期望完全无代理、无邮箱基础设施的“一键白嫖”  
- 把本项目当官方 xAI 商业方案  

---

## 核心优势

### 1) 换票一次做对：授权码 + `referrer=grok-build`

这是和大量“SSO→token 脚本”的本质区别。

| 错误做法 | 结果 |
|----------|------|
| 直接拿网页 SSO cookie 当网关凭证 | 权限面不对，寿命短 / 权限残 |
| Device Flow 换 token | 常常 **没有** `referrer=grok-build` claim |
| base_url 落到 `api.x.ai` | 走计费通道，免费 chat 直接拒绝 |

GrokPool 固定走：

- Authorization Code + PKCE  
- authorize / consent **双处**注入 `referrer=grok-build`  
- 换完 **硬校验 JWT claim**  
- `base_url = https://cli-chat-proxy.grok.com/v1`  

缺 claim 直接拒绝入库，避免“导入成功但全员 permission-denied”的假繁荣。

### 2) 为「多小号」重做调度，不是套大号逻辑

免费号特征是：**数量多、单窗约 1M token、很容易被残血号坑。**

GrokPool 在 Sub2API 上补了：

- 剩余 token headroom 参与选号  
- 有空闲免费号时减少无意义排队（TopK overflow / free-slot）  
- sticky 会话在账号明显不合适时可逃逸  
- 免费号 429 冷却更克制，避免整池假死  
- 死号不参与调度，且不误伤 OAuth 刷新路径  

目标不是“理论最优”，而是：**少把请求打到空号，少在还有余量时排队。**

### 3) 注册 → 转换 → 入库全自动

一条日志链看懂状态：

```text
[CPA] OK user@mail -> xai-....json
[SUB2] PUSH OK user@mail · mode=cpa-data created=1 ...
```

- 注册机面板一键跑  
- 成功即本地 OAuth 落盘  
- 默认自动 POST 进 Sub2（也可手动导 ZIP）  
- 可绑定指定分组  

不用再：导出 cookie → 手改 JSON → 管理后台一点点点导入。

### 4) 死号可运营，不靠玄学

管理端支持批量：

- **探测死号**  
- 仅把上游真封禁 / chat 永久 permission-denied 等判死  
- **不把** billing 403、临时 CF、429 直接当死号删掉  

池子用久了，能清、能留、能看明白，而不是整页 unknown。

### 5) 客户端友好：标准 OpenAI 形态

对 CC Switch / Claude Code / 任何 OpenAI SDK：

```text
base_url = http://127.0.0.1:18080/v1
api_key  = Sub2API Token
model    = 分组放行的 grok 模型名
```

不用塞 SSO，不用改 Anthropic 报文，不用每个客户端适配 xAI 私有细节。

### 6) 代码与 Docker 可对齐、可复现

本项目强调：

```text
仓库源码  ==  镜像 grokpool-sub2api:local  ==  正在跑的容器
```

提供：

- `scripts/build-sub2api.ps1` 一键构建并重启  
- `scripts/check-parity.ps1` 检查运行中二进制是否包含死号/OAuth/cli-chat-proxy 等关键能力  

避免“我本地改了但容器还是上游旧逻辑”这种慢性自杀。

### 7) Windows 现实环境可落地

- 注册机留在宿主机（Camoufox/Chromium 比硬塞进 Docker 稳）  
- Sub2 走 Docker Compose（Postgres + Redis + 自定义镜像）  
- 明确要求 Clash **TUN / 虚拟网卡**，解决 Docker 出网不走系统代理的坑  
- 文档按实战写：端口、分组、Token、CC Switch 字段、失败怎么排  

### 8) 安全默认更干净

- `deploy/.env`、token、账号文件默认 gitignore  
- worker 不从脚本硬编码管理员密码  
- OAuth / SSO 原材料不进仓库  

---

## 和其他方案比

| 维度 | 只丢 SSO 给反代 | 普通 SSO→token 脚本 | 原版 Sub2API | **GrokPool** |
|------|-----------------|---------------------|--------------|--------------|
| 凭证类型 | 网页 SSO | 常缺 grok-build claim | 通用 OAuth | **强制 Build OAuth** |
| 导入后 chat | 易 403 | 易全军 permission-denied | 取决于你怎么导 | **claim + base_url 校验** |
| 多免费号调度 | 无/很糙 | 无 | 通用逻辑 | **1M 窗剩余感知** |
| 死号治理 | 基本没有 | 无 | 弱 | **批量探测 + 谨慎判定** |
| 注册到入库 | 手动 | 半自动 | 手动导入 | **注册后自动推** |
| CC Switch | 自己琢磨 | 自己琢磨 | 需摸索 | **OpenAI 格式文档** |
| 本地复现 | 看运气 | 看运气 | 上游镜像 | **自定义镜像 + parity 检查** |

一句话：  
**GrokPool 把“能注册”升级成“能稳定进池、能调度、能给客户端用、能维护”。**

---

## 架构

```text
┌────────────────────────────┐     SSO cookie      ┌──────────────────────────┐
│  Register (host Windows)   │ ─────────────────► │  sso2cpa_core            │
│  Camoufox / Chromium       │                    │  Authorization Code+PKCE │
│  panel :8877               │                    │  referrer=grok-build     │
└────────────────────────────┘                    └────────────┬─────────────┘
                                                               │ type=oauth JSON
                                                               ▼
                                                    ┌──────────────────────────┐
                                                    │  Sub2API Docker :18080   │
                                                    │  image: grokpool-sub2api │
                                                    │  cli-chat-proxy upstream │
                                                    │  free-tier scheduler     │
                                                    │  dead-account probe      │
                                                    └────────────┬─────────────┘
                                                                 │ OpenAI /v1
                                                                 ▼
                                                    ┌──────────────────────────┐
                                                    │  CC Switch / Claude Code │
                                                    │  base_url .../v1         │
                                                    │  api_key = Sub2 token    │
                                                    └──────────────────────────┘
```

- **注册机跑在宿主机**（浏览器自动化，不进 Docker）
- **Sub2API 跑在 Docker**（本仓库自定义镜像，不是上游原版 latest）
- 注册成功后默认：`[CPA] OK` → `[SUB2] PUSH OK mode=cpa-data`

---

## 功能一览

| 模块 | 能力 |
|------|------|
| 注册机 | Camoufox 无头 / Chromium；多邮箱源；birth+TOS 激活；SSO→OAuth |
| 自动入库 | 授权码换 `grok-build` token，推 Sub2 `type=oauth` |
| Sub2 调度 | 免费号剩余 token 感知、软适配、429 冷却、有空闲号少排队 |
| 死号 | 管理端批量探测；chat permission-denied 等真死才标死；不误杀 billing 403 |
| 客户端 | OpenAI 兼容网关，给 CC Switch / Claude Code 用 |

补丁清单见 [`docs/PATCHES.md`](docs/PATCHES.md)。

---

## 环境要求

| 项 | 说明 |
|----|------|
| OS | Windows 10/11（注册机）；Docker 可用的环境 |
| Docker Desktop | 已启动 |
| Python | 3.10+（注册机 venv） |
| 代理 | Clash Verge / Mihomo / CFW 等，**建议 TUN / 虚拟网卡 / 增强模式** |
| 浏览器 | Chrome/Edge（Chromium 引擎）；Camoufox 首次自动下载 |
| 邮箱 | 自建 CF Worker 域 / DuckMail 等；公共临时邮极易被 xAI 拒 |

---

## 快速开始（Windows）

### 0. 拿到代码

```text
GrokPool/
  sub2api/          # 已打 GrokPool 补丁的 Sub2API
  register-win/     # 注册机（junction 或拷贝 grok-register-win）
  deploy/           # docker compose
  worker/           # 一键开面板自动推 Sub2
  docs/
  scripts/
```

若 `register-win` 不存在，创建目录联接（路径按你本机改）：

```bat
mklink /J D:\Projects\GrokPool\register-win D:\Projects\Grok注册机\grok-register-win
```

### 1. 代理先开 TUN

见下一节。没开穿透就先别往下跑。

### 2. 启动 Sub2API

```powershell
cd D:\Projects\GrokPool\deploy
copy .env.example .env
# 编辑 .env：ADMIN_PASSWORD / POSTGRES_PASSWORD / JWT_SECRET / SERVER_PORT=18080
notepad .env

cd ..
powershell -ExecutionPolicy Bypass -File .\scripts\build-sub2api.ps1
```

浏览器打开：http://127.0.0.1:18080  
用 `.env` 里的 `ADMIN_EMAIL` / `ADMIN_PASSWORD` 登录。

### 3. 启动注册面板（自动推送）

```bat
D:\Projects\GrokPool\start_worker.bat
```

或：

```bat
D:\Projects\GrokPool\worker\start_worker.bat
```

- 面板：http://127.0.0.1:8877  
- 选邮箱源并保存  
- 引擎 Camoufox 无头  
- 可选：选 Sub2 分组  
- 填数量 → 开始  
- 日志应出现：
  - `[CPA] OK user@mail -> xai-....json`
  - `[SUB2] PUSH OK ... mode=cpa-data ...`

### 4. 在 Sub2 建分组 + API Token

1. Admin → 分组：新建 `grok` 平台分组  
2. 确认账号 `type=oauth`，已进组  
3. 用户/密钥：创建 API Token，绑定该组  

### 5. CC Switch 接 OpenAI 格式

见 [CC Switch 专节](#cc-switch--claude-codeopenai-格式) 与 [`docs/CCSWITCH.md`](docs/CCSWITCH.md)。

---

## Sub2API Docker 详细教程

### 为什么必须用本仓库镜像

上游 `weishaw/sub2api:latest` **没有**：

- 免费 Grok 调度优化  
- 死号探测 API/UI  
- SSO→OAuth 授权码 + `referrer=grok-build` 硬校验  

本项目镜像名：

```text
grokpool-sub2api:local
```

由 `sub2api/Dockerfile` 构建，前后端 embed 进同一二进制。

### 文件

| 文件 | 作用 |
|------|------|
| `deploy/docker-compose.yml` | sub2api + postgres + redis |
| `deploy/.env.example` | 环境变量模板 |
| `deploy/.env` | 本地密钥（**勿提交**） |
| `scripts/build-sub2api.ps1` | 构建镜像并 recreate 容器 |
| `scripts/check-parity.ps1` | 检查运行中容器是否含关键字符串 |

### 首次启动

```powershell
cd D:\Projects\GrokPool\deploy
copy .env.example .env
```

至少改这些：

```env
SERVER_PORT=18080
ADMIN_EMAIL=admin@sub2api.local
ADMIN_PASSWORD=你的强密码
POSTGRES_PASSWORD=你的数据库密码
JWT_SECRET=用 openssl rand -hex 32 生成
RUN_MODE=simple
```

```powershell
cd D:\Projects\GrokPool
powershell -ExecutionPolicy Bypass -File .\scripts\build-sub2api.ps1
docker compose -f deploy\docker-compose.yml --env-file deploy\.env ps
```

### 日常命令

```powershell
# 看状态
docker compose -f deploy\docker-compose.yml ps

# 日志
docker logs sub2api --tail 100 -f

# 停
docker compose -f deploy\docker-compose.yml down

# 改了 sub2api 代码后
powershell -ExecutionPolicy Bypass -File .\scripts\build-sub2api.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\check-parity.ps1
```

### 健康检查

```powershell
# 管理页
start http://127.0.0.1:18080

# 路由存在性（401 表示路由在，只是没登录）
curl -X POST http://127.0.0.1:18080/api/v1/admin/grok/accounts/probe-dead
```

### 数据卷

- `sub2api_data`：应用数据 / 配置  
- `sub2api_pg`：Postgres  

`down -v` 会删库，别随手加 `-v`。

---

## 代理：必须开穿透 / 虚拟网卡

本地 Sub2 + 注册机同时在跑时，**只开系统代理 / 浏览器代理往往不够**。

### 原因

| 进程 | 不走 TUN 时常见情况 |
|------|---------------------|
| 注册机 Python / Camoufox | 可手动设 `http://127.0.0.1:789x` |
| Docker 容器出网 | **经常不走**系统代理，直连家宽/机房 IP |
| Sub2 刷新 token / 上游 chat | 容器出网脏 IP → CF / 风控 / 偶发 403 |

### 推荐设置（Clash Verge / Mihomo）

1. 打开 **TUN Mode / 虚拟网卡 / 增强模式 / 服务模式**（名称因客户端而异）  
2. 允许 Windows 安装虚拟网卡驱动（首次需管理员）  
3. 规则里确保 `grok.com` / `x.ai` / `cli-chat-proxy.grok.com` 走代理节点  
4. 选稳定住宅/干净节点（别拿被烧红的机房 IP 硬刚）  
5. 注册机 `config.json` / 环境变量：

```json
{
  "proxy": "http://127.0.0.1:7895",
  "allow_proxy_fallback": true
}
```

端口按你本机 Clash 改（7890 / 7895 / 7897 都常见）。

### 可选：给容器显式代理

一般 **优先 TUN**。若必须：

```env
# deploy/.env
UPDATE_PROXY_URL=http://host.docker.internal:7895
```

注意：不是所有出站路径都读这个变量；TUN 仍然更省心。

### 自检

```text
# 宿主机出口
curl https://api.ip.sb/ip

# 注册日志里应看到出口探测 / 代理地址
# Sub2 账号探测不应大面积 Cloudflare HTML
```

---

## 注册机面板

### 启动方式

| 方式 | 命令 | 说明 |
|------|------|------|
| GrokPool Worker | `start_worker.bat` | 默认开 `AUTO_SUB2_PUSH`、Camoufox、读 `deploy/.env` 密码 |
| 注册机自带 | `register-win\start.bat` | 仅面板；推送取决于环境变量 |

### 面板地址

- Worker 默认：http://127.0.0.1:8877  
- 部分机器 8787 被占用，以窗口打印为准  

### 推荐流程

1. 邮箱源：优先 **CF Worker 自建域** / DuckMail；少用公共 temp  
2. 浏览器：Camoufox 无头（或 Chromium 有头调试）  
3. 代理：本机 Clash 端口  
4. Sub2 分组：面板里选中目标组（自动 bind）  
5. 开始注册  
6. 成功标志：
   - 本地 `data/cpa/xai-*.json` 生成  
   - 日志 `[SUB2] PUSH OK mode=cpa-data`  
   - Sub2 账号列表出现 `oauth` 号  

### 手动导出（不自动推时）

面板下载：

- **SSO TXT**：`email----password----sso`  
- **CPA ZIP**：CLIProxyAPI 用 OAuth JSON  
- **Sub2 ZIP**：`all.json` + `grok-*.json`，Admin → 导入数据  

### 环境变量（Worker / panel）

| 变量 | 默认 | 含义 |
|------|------|------|
| `AUTO_CPA` | `1` | SSO→OAuth 转换 |
| `AUTO_SUB2_PUSH` | `1` | 转换后推 Sub2 |
| `SUB2_IMPORT_MODE` | `cpa-data` | `cpa-data` 或 `sso-to-oauth` |
| `SUB2API_BASE_URL` | `http://127.0.0.1:18080` | Sub2 地址 |
| `SUB2API_ADMIN_EMAIL` | `admin@sub2api.local` | 登录邮箱 |
| `SUB2API_ADMIN_PASSWORD` | 从 `deploy/.env` 读 | 登录密码 |
| `GROK_PROXY` | `http://127.0.0.1:7895` | 注册/换票代理 |
| `GROK_BROWSER_ENGINE` | `camoufox` | 浏览器引擎 |
| `PANEL_PORT` | `8877` | 面板端口 |

---

## Cloudflare 自建临时邮箱（推荐）

公共临时域（如部分 duckmail / tempmail）容易被 xAI 整域拒绝。  
**推荐用 Cloudflare + 自己的域名** 搭临时邮（无需 VPS）。

### 完整教程

👉 **[docs/CF_TEMP_EMAIL.md](docs/CF_TEMP_EMAIL.md)**

内容包括：

- 免费域名怎么接（示例面板：https://my.dnshe.com/clientarea.php ）
- Cloudflare 添加站点 / 改 NS
- Email Routing（MX）怎么开
- `cloudflare_temp_email` Worker 部署（可复制提示词让 AI 代部署）
- Catch-all 指到 Worker
- `register-win` / `register-cpa` 的 `config.json` 字段
- 验收清单与排错

### 最短路径

```text
1. 域名 NS → Cloudflare（Active）
2. Email Routing 启用（出现 MX）
3. 部署 https://github.com/dreamhunter2333/cloudflare_temp_email
4. Catch-all → 你的 Worker
5. 注册机填 cloudflare/cfworker API + admin + 域名
```

### 注册机关键字段（示例）

```json
{
  "email_provider": "cfworker",
  "cfworker_api_url": "https://mail-api.你的域名.com",
  "cfworker_admin_token": "你的ADMIN密码",
  "cfworker_domain": "你的域名.com",
  "cloudflare_api_base": "https://mail-api.你的域名.com",
  "cloudflare_api_key": "你的ADMIN密码",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address",
  "defaultDomains": "你的域名.com"
}
```

不会部署？把 [docs/CF_TEMP_EMAIL.md](docs/CF_TEMP_EMAIL.md) 里的「让 AI 帮你部署」提示词整段丢给 AI，把域名换成你的即可。

---

## OAuth 硬规则（必读）

详细：[`docs/OAUTH.md`](docs/OAUTH.md)

1. **SSO 不能直接当 Sub2 凭证**  
2. 必须 **Authorization Code + PKCE**  
3. authorize **和** consent 都要 `referrer=grok-build`  
4. 换完校验 JWT claim，不是 `grok-build` 就拒绝入库  
5. `base_url` 必须是 `https://cli-chat-proxy.grok.com/v1`  

缺 claim 时上游表现：

```text
permission-denied
Access to the chat endpoint is denied
```

这和「号死了」很像，其实是 **换票方式错了**。本仓库已在：

- `register-win/lib/sso2cpa_core.py`  
- `sub2api/backend/internal/pkg/xai/sso_device.go`  

两侧统一成授权码流程。

---

## 死号探测

1. 打开 Sub2 Admin → 账号  
2. 勾选 Grok 账号（可本页全选）  
3. 批量操作 → **探测死号**  
4. 可选删除确认的死号  

判定：

- **死号**：chat 永久 permission-denied / 凭证彻底失效等  
- **不是死号**：billing 403 抖动、CF 临时页、429  

调度侧不会优先使用已标记不可调度的死号。

API：

```http
POST /api/v1/admin/grok/accounts/probe-dead
```

---

## CC Switch / Claude Code（OpenAI 格式）

完整说明：[`docs/CCSWITCH.md`](docs/CCSWITCH.md)

### 关键点

GrokPool 对外是 **OpenAI 兼容接口**，不是 Anthropic 原生，也不是 xAI 网页 SSO。

### CC Switch 填写示例

| 项 | 值 |
|----|----|
| 协议 / 供应商 | **OpenAI** |
| API Base URL | `http://127.0.0.1:18080/v1` |
| API Key | Sub2 里创建的 Token（`sk-...` 或你实例格式） |
| Model | 分组里放行的模型名，如 `grok-4` |
| 路径 | 默认 Chat Completions（`/chat/completions`） |

```json
{
  "api_type": "openai",
  "base_url": "http://127.0.0.1:18080/v1",
  "api_key": "sk-your-sub2api-token",
  "model": "grok-4"
}
```

### 冒烟测试

```bat
curl http://127.0.0.1:18080/v1/chat/completions ^
  -H "Authorization: Bearer sk-your-sub2api-token" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"grok-4\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

### 常见错配

| 现象 | 原因 |
|------|------|
| 401 | Token 错 / 没带 Bearer |
| 模型不存在 | 分组未放行该 model |
| 上游 permission-denied | 账号不是 grok-build OAuth 或已死 |
| 连不上 | Sub2 没起 / 端口不是 18080 / 防火墙 |

---

## 代码与 Docker 一致性

保证「你 Git 里的代码」=「Docker 正在跑的」：

```powershell
# 1) 用当前 sub2api/ 源码构建并重启
powershell -ExecutionPolicy Bypass -File .\scripts\build-sub2api.ps1

# 2) 校验容器二进制包含关键能力
powershell -ExecutionPolicy Bypass -File .\scripts\check-parity.ps1
```

`check-parity.ps1` 会确认运行中二进制包含：

- `probe-dead` / `ClassifyGrokAccountLiveness`  
- `authorization_code` / `missing referrer=grok-build`  
- `cli-chat-proxy.grok.com` / `sso-to-oauth`  

容器应显示：

```text
IMAGE: grokpool-sub2api:local
PORTS: 0.0.0.0:18080->8080/tcp
STATUS: healthy
```

改调度 / OAuth / 死号任何 Go 或前端代码后，**必须重新 build 镜像**，只改磁盘源码不会热更新容器。

---

## 目录结构

```text
GrokPool/
├── README.md                 # 本文件
├── AGENTS.md                 # 给 AI/协作者的短简报
├── .gitignore
├── start_worker.bat          # 一键开注册 Worker
├── deploy/
│   ├── docker-compose.yml    # Sub2 本地栈
│   ├── .env.example
│   └── .env                  # 本地密钥（忽略提交）
├── scripts/
│   ├── build-sub2api.ps1     # 构建=运行
│   └── check-parity.ps1      # 一致性检查
├── docs/
│   ├── CF_TEMP_EMAIL.md      # Cloudflare 自建临时邮箱（含免费域名）
│   ├── OAUTH.md
│   ├── CCSWITCH.md
│   ├── SCHEDULER.md
│   ├── PATCHES.md
│   ├── WORKFLOW.md
│   └── NOTES_FOR_AI.md
├── scripts/
│   ├── build-sub2api.ps1
│   ├── check-parity.ps1
│   ├── promote-to-main.ps1
│   └── deploy-cf-temp-email.ps1   # CF 临时邮辅助部署（需 wrangler login）
├── worker/
│   ├── start_worker.bat
│   └── README.md
├── sub2api/                  # 打过补丁的 Sub2API 源码 + Dockerfile
└── register-win/             # 注册机（junction → grok-register-win）
    ├── panel/app.py
    ├── lib/sso2cpa_core.py
    ├── grok_register_ttk.py
    ├── start.bat
    └── config.example.json
```

---

## 配置说明

### deploy/.env（Sub2）

见 `deploy/.env.example`。生产务必改掉所有 `change-me-*`。

### register-win/config.json

由 `config.example.json` 复制而来（忽略提交）。常用字段：

```json
{
  "proxy": "http://127.0.0.1:7895",
  "allow_proxy_fallback": true,
  "browser_engine": "camoufox",
  "enable_nsfw": true,
  "email_provider": "cfworker",
  "register_count": 1,
  "round_timeout_sec": 480
}
```

---

## 常见问题

**Q: 导入后马上 chat 403？**  
A: 先看 access_token 是否 `referrer=grok-build`、base_url 是否 cli-chat-proxy。老 device-flow 号直接删掉重导。

**Q: 注册成功但没有 PUSH OK？**  
A: 检查 `AUTO_SUB2_PUSH=1`、`SUB2API_ADMIN_PASSWORD`、Sub2 是否 healthy、面板日志 `[SUB2] PUSH FAIL`。

**Q: Docker 里还是旧逻辑？**  
A: 你跑的不是 `grokpool-sub2api:local` 或没 rebuild。执行 `scripts\build-sub2api.ps1` + `check-parity.ps1`。

**Q: 公共临时邮箱一直超时/被拒？**  
A: 换自建域名邮箱。Gmail 别名无效。

**Q: CC Switch 应该填 Anthropic 还是 OpenAI？**  
A: **OpenAI**。Base `http://127.0.0.1:18080/v1`。

**Q: 代理开了系统代理还是不稳？**  
A: 开 **TUN / 虚拟网卡**，让 Docker 出网也走代理。

**Q: 死号按钮点了 deleted=0？**  
A: 可能只是 unknown/软失败；只有确认 dead 才删。billing 403 不会当死号。

---

## 上传 GitHub 注意

### 不要提交

- `deploy/.env`  
- `register-win/config.json` / `token.json` / `mail_credentials.txt`  
- `register-win/data/cpa/*` / `accounts_*.txt`  
- 任何 access_token / refresh_token / 密码  

### 嵌套 git

当前常见布局：

- `GrokPool/` 作为总仓（建议在此 `git init`）  
- `sub2api/` 可能仍带上游 `.git`  
- `register-win/` 可能是 junction 到独立仓库  

发布 **单仓 monorepo** 时建议：

```powershell
# 谨慎：会去掉子仓独立 git 历史，仅在你确认要 monorepo 时做
# Remove-Item -Recurse -Force sub2api\.git
```

或改用 **submodule** 跟踪两个子项目。  
junction 目标若不在仓库内，GitHub 上不会包含注册机文件——发布前改为真实拷贝或 submodule。

### 建议提交内容

- 全部 `docs/`、`deploy/*.example`、`scripts/`、`sub2api/` 补丁源码  
- `register-win` 源码（无密钥）  
- 本 README  

### 推送前自检

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check-parity.ps1
git status   # 确认没有 .env / token.json
```

---

## 许可证

- `sub2api/`：遵循其上游仓库 LICENSE  
- 注册机：见 `register-win/LICENSE`  
- 本集成层文档与脚本：与仓库主 LICENSE 一致；若无单独声明，默认仅供学习研究使用  

---

## 维护者备忘

```text
改 Go/前端 → build-sub2api.ps1 → check-parity.ps1
改注册机 Python → 重启 start_worker.bat 即可（无需重建镜像）
OAuth 问题 → docs/OAUTH.md
客户端问题 → docs/CCSWITCH.md
调度问题 → docs/SCHEDULER.md
```
