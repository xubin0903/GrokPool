# Cloudflare 自建临时邮箱教程（Grok 注册用）

本教程教你用 **Cloudflare + 一个域名** 搭可收验证码的临时邮箱，并接到 GrokPool 注册机。  
**不需要 VPS、不需要本机 25 端口。**

适用仓库：

- 注册机：`register-win/`（Windows 面板）
- 对照注册机：`register-cpa/`（CPA 风格）

推荐项目：

- https://github.com/dreamhunter2333/cloudflare_temp_email

---

## 你能得到什么

注册时自动生成类似：

```text
gxxxxx@你的域名.com
```

xAI 验证码邮件进 Cloudflare → Worker 存进 D1 → 注册机 API 拉取验证码。

比公共 `tempmail` / `duckmail.sbs` 更不容易整域被拉黑（仍不保证永久安全）。

---

## 需要准备

| 项目 | 说明 |
|------|------|
| Cloudflare 账号 | https://dash.cloudflare.com 免费即可 |
| 一个域名 | 可以是付费域名，也可以是免费域名（见下） |
| 本机 Node.js（可选） | 命令行部署 Worker 时用；也可让 AI/按 UI 部署 |

### 免费域名哪里弄

用户常用免费域名面板（示例）：

- https://my.dnshe.com/clientarea.php

在里面注册/领取免费域名后，把该域名的 **DNS / 名称服务器** 指到 Cloudflare（见下一步）。  
不同免费域后缀规则不一，选能 **自定义 NS** 的那种。

付费域名（`.com` / `.xyz` 等）同样可以，流程一样。

---

## 总流程（5 步）

```text
1. 域名 NS 接到 Cloudflare
2. 开启 Email Routing（自动加 MX）
3. 部署 cloudflare_temp_email Worker
4. Catch-all 邮件转到 Worker
5. 填写注册机 config，开始注册
```

---

## 步骤 1：域名接入 Cloudflare

1. 打开 https://dash.cloudflare.com 登录  
2. **添加站点** → 输入你的域名 → 选 **Free**  
3. CF 给你两条 NS，类似：

```text
xxx.ns.cloudflare.com
yyy.ns.cloudflare.com
```

4. 去域名注册商 / 免费域名面板（如 dnshe 客户端区）  
   找到该域名的 **NameServers / 名称服务器**  
   改成 CF 这两条并保存  

5. 回到 CF，等到域名状态变成 **有效 / Active**（几分钟到几小时）

> Active 之前不要测收信。

---

## 步骤 2：开启 Email Routing（收信 MX）

域名 **Active** 后：

1. 点进域名 → 左侧 **Email** → **Email Routing**  
2. **启用 Email Routing**  
3. 确认 DNS 里出现类似记录（名称以你的域名为准）：

| 类型 | 名称 | 内容 |
|------|------|------|
| MX | `@` | `route1.mx.cloudflare.net`（优先级按页面） |
| MX | `@` | `route2.mx.cloudflare.net` |
| MX | `@` | `route3.mx.cloudflare.net` |
| TXT | `@` | `v=spf1 include:_spf.mx.cloudflare.net ~all` |
| TXT | `cf2024-1._domainkey` | DKIM（CF 生成） |

这些记录 **不要** 当成网页橙云代理去纠结；MX 本身不是网站代理。

此时：发到 `任意@你的域名` 的信会进入 Cloudflare 邮件系统。  
**还没进 Worker 前，信可能被 drop**——下一步部署并绑定 Worker。

---

## 步骤 3：部署临时邮 Worker

项目：

https://github.com/dreamhunter2333/cloudflare_temp_email

### 方式 A：让 AI 帮你部署（推荐小白）

把下面整段复制给 AI（Claude / Cursor 等），并说明你的域名：

```text
请按 https://github.com/dreamhunter2333/cloudflare_temp_email 官方文档，
用 wrangler 在我的 Cloudflare 账号上部署 Worker 临时邮箱。

我的域名：你的域名.com
要求：
1) 创建 D1 数据库并执行 db/schema.sql 及 db 下补丁
2) Worker 变量：
   - DOMAINS / DEFAULT_DOMAINS = ["你的域名.com"]
   - ADMIN_PASSWORDS = ["随机强密码"]
   - JWT_SECRET = 随机字符串
   - ENABLE_USER_CREATE_EMAIL = true
3) 自定义域绑到 mail-api.你的域名.com（或 workers.dev）
4) 部署完成后输出：
   - API Base URL
   - admin 密码
   - 如何把 Email Routing Catch-all 指到该 Worker
不要把密码提交到 git。
```

本仓库也提供脚本骨架（需本机已 `wrangler login`）：

```powershell
# 可选：先 clone 官方项目到 tools/cloudflare_temp_email
git clone --depth 1 https://github.com/dreamhunter2333/cloudflare_temp_email.git tools/cloudflare_temp_email

# 登录 Cloudflare（浏览器授权一次）
wrangler login

# 按脚本提示部署（脚本在 scripts/，可按你的域名改）
powershell -ExecutionPolicy Bypass -File scripts\deploy-cf-temp-email.ps1
```

> 脚本会写本地密钥目录 `tools/cf-temp-email-secrets/`（已 gitignore）。

### 方式 B：命令行自己部署（摘要）

在 `cloudflare_temp_email/worker` 目录：

```bash
npm install --legacy-peer-deps
wrangler login
wrangler d1 create your-temp-email-db
# 把 database_id 写入 wrangler.toml
wrangler d1 execute your-temp-email-db --remote --file=../db/schema.sql
# 再按顺序执行 db/ 下 20*.sql 补丁
# 编辑 wrangler.toml：DOMAINS / DEFAULT_DOMAINS / ADMIN_PASSWORDS / JWT_SECRET
wrangler deploy
```

部署成功后应有 API 地址，例如：

```text
https://mail-api.你的域名.com
```

或：

```text
https://xxx.workers.dev
```

### 冒烟测试（建号）

```bash
curl -X POST "https://mail-api.你的域名.com/admin/new_address" ^
  -H "Content-Type: application/json" ^
  -H "x-admin-auth: 你的ADMIN密码" ^
  -d "{\"enablePrefix\":true,\"name\":\"test1\",\"domain\":\"你的域名.com\"}"
```

成功会返回 `address` + `jwt`，地址后缀是你的域名。

---

## 步骤 4：Catch-all 指到 Worker（必须）

否则邮件进 CF 后被丢掉，注册机会一直等验证码。

### 控制台点法

1. Cloudflare → 你的域名  
2. **Email** → **Email Routing** → **Routing rules**  
3. **Catch-all**（或“所有邮件”）  
4. 动作选 **Send to a Worker**  
5. Worker 选你部署的名字（例如 `grokpool-temp-email`）  
6. 启用并保存  

### 命令行（若 wrangler 支持）

```bash
# 部分账号 catch-all 仅允许 forward/drop；若失败请用控制台选 Worker
wrangler email routing rules get 你的域名.com catch-all
```

也可用 Cloudflare API 更新 catch-all 的 `actions` 为：

```json
{ "type": "worker", "value": ["你的worker名"] }
```

**做完后 Catch-all 应为：Enabled + worker: 你的 Worker。**

---

## 步骤 5：接到 GrokPool 注册机

### Windows 注册机 `register-win/config.json`

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
  "cloudflare_path_messages": "/api/mails",
  "cloudflare_path_token": "/api/token",
  "cloudflare_path_domains": "/api/domains",
  "defaultDomains": "你的域名.com",
  "proxy": "http://127.0.0.1:7895"
}
```

面板邮箱下拉选 **cfworker**（或 cloudflare，两者字段兼容）。

### CPA 注册机 `register-cpa/config.json`

```json
{
  "email_provider": "cloudflare",
  "cloudflare_api_base": "https://mail-api.你的域名.com",
  "cloudflare_api_key": "你的ADMIN密码",
  "cloudflare_auth_mode": "x-admin-auth",
  "cloudflare_path_accounts": "/admin/new_address",
  "cloudflare_path_messages": "/api/mails",
  "cloudflare_path_token": "/api/token",
  "defaultDomains": "你的域名.com",
  "proxy": "http://127.0.0.1:7895",
  "cpa_auto_add": true
}
```

> `config.json` 含密钥，**不要提交 git**（仓库已 ignore）。

重启注册面板后再开跑。日志里邮箱应类似：`gxxxx@你的域名.com`。

---

## 免费域名（dnshe）补充说明

入口示例：https://my.dnshe.com/clientarea.php

1. 登录客户端  
2. 注册/领取免费域名  
3. 在域名管理里找到 **DNS / 名称服务器**  
4. 改为 Cloudflare 提供的两条 NS  
5. 回到本教程步骤 2～5  

注意：

- 有的免费域 **不允许改 NS** → 换一个能改 NS 的  
- 免费域可能被批量滥用，不如自购冷门域干净，但比公共 temp 通常好  
- 解析生效后，在 CF 面板确认域名 **Active**

---

## 常见问题

**Q: 建号 API 200，但注册一直等验证码？**  
A: 99% 是 Catch-all 没指到 Worker，或仍是 drop。去 Email Routing 检查。

**Q: 只要收信，要不要 VPS？**  
A: CF 临时邮 **不要 VPS**。CloudMail 那种 Docker 自建才要服务器。

**Q: 本机能不能当邮箱服务器？**  
A: 不建议。家宽常无公网 IP / 封 25。CF 方案就是为了避开这个。

**Q: ADMIN 密码丢了？**  
A: 改 Worker 的 `ADMIN_PASSWORDS` 变量后重新 `wrangler deploy`。

**Q: 会不会还被 xAI 拒域名？**  
A: 自有域通常好很多，不保证。被拒就换域或加强注册指纹/代理。

**Q: 和 MailNest 比？**  

| | CF 自建 | MailNest 临时 Outlook |
|--|---------|------------------------|
| 域名 | 要（可免费） | 不要 |
| 费用 | CF 免费套餐 + 域名 | 按次 |
| 部署 | 要 Worker + Routing | 填 API Key 即可 |
| 可控性 | 高 | 中 |

---

## 安全提醒

- 不要把 `ADMIN_PASSWORDS`、`JWT_SECRET`、`config.json` 推进公开仓库  
- 本仓库 ignore：`tools/cf-temp-email-secrets/`、`**/config.json`（注册机）  
- 公开文档只写步骤，不写真实密码  

---

## 验收清单

- [ ] 域名在 CF 状态 Active  
- [ ] DNS 有 3 条 MX + SPF/DKIM  
- [ ] Worker 已部署，API 可访问  
- [ ] Catch-all → 你的 Worker 且 Enabled  
- [ ] `/admin/new_address` 能返回 `@你的域名`  
- [ ] 注册机 config 已填 API + admin + domain  
- [ ] 实跑注册能收到 xAI 验证码  

全部勾完就可以稳定用 CF 邮箱跑 Grok 注册。
