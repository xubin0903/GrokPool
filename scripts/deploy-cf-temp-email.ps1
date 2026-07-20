# Deploy dreamhunter2333/cloudflare_temp_email worker for daboo.cc.cd
# Run:
#   powershell -ExecutionPolicy Bypass -File D:\Projects\GrokPool\scripts\deploy-cf-temp-email.ps1
#
# First time: browser login to Cloudflare (wrangler login).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Repo = Join-Path $Root "tools\cloudflare_temp_email"
$Worker = Join-Path $Repo "worker"
$Domain = "daboo.cc.cd"
$DbName = "grokpool-temp-email-db"
$WorkerName = "grokpool-temp-email"
$AdminPass = -join ((48..57 + 65..90 + 97..122 | Get-Random -Count 16 | ForEach-Object { [char]$_ }))
$JwtSecret = -join ((48..57 + 97..122 | Get-Random -Count 32 | ForEach-Object { [char]$_ }))
$SecretsDir = Join-Path $Root "tools\cf-temp-email-secrets"
New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null

if (-not (Test-Path $Worker)) {
  throw "repo missing: $Repo — clone cloudflare_temp_email first"
}

Write-Host "==> ensure wrangler"
Push-Location $Worker
try {
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm not found" }
  npm install --no-fund --no-audit 2>&1 | Out-Host

  Write-Host "==> Cloudflare login (browser). Complete it then return here."
  npx wrangler whoami 2>&1 | Out-Host
  $who = (npx wrangler whoami 2>&1 | Out-String)
  if ($who -notmatch "You are logged in|Account Name|email") {
    npx wrangler login
  }

  Write-Host "==> create D1 database: $DbName"
  $createOut = npx wrangler d1 create $DbName 2>&1 | Out-String
  Write-Host $createOut
  $dbId = $null
  if ($createOut -match 'database_id\s*=\s*"([^"]+)"') { $dbId = $Matches[1] }
  elseif ($createOut -match '([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})') { $dbId = $Matches[1] }
  if (-not $dbId) {
    # maybe already exists — list
    $list = npx wrangler d1 list 2>&1 | Out-String
    Write-Host $list
    if ($list -match "$DbName\s+([0-9a-f-]{36})") { $dbId = $Matches[1] }
  }
  if (-not $dbId) { throw "cannot parse D1 database_id. Create manually in CF dashboard and re-run." }
  Write-Host "D1 id = $dbId"

  Write-Host "==> write wrangler.toml"
  $toml = @"
name = "$WorkerName"
main = "src/worker.ts"
compatibility_date = "2025-04-01"
compatibility_flags = [ "nodejs_compat" ]
keep_vars = true

[vars]
PREFIX = "g"
DEFAULT_DOMAINS = ["$Domain"]
DOMAINS = ["$Domain"]
JWT_SECRET = "$JwtSecret"
BLACK_LIST = ""
ENABLE_USER_CREATE_EMAIL = true
ENABLE_USER_DELETE_EMAIL = true
ENABLE_AUTO_REPLY = false
ADMIN_PASSWORDS = ["$AdminPass"]
DISABLE_ADMIN_PASSWORD_CHECK = false

[[d1_databases]]
binding = "DB"
database_name = "$DbName"
database_id = "$dbId"
"@
  Set-Content -Path (Join-Path $Worker "wrangler.toml") -Value $toml -Encoding utf8

  Write-Host "==> init D1 schema (schema + patches)"
  $dbDir = Join-Path $Repo "db"
  $files = @("schema.sql") + (Get-ChildItem $dbDir -Filter "20*.sql" | Sort-Object Name | ForEach-Object { $_.Name })
  foreach ($f in $files) {
    $path = Join-Path $dbDir $f
    if (-not (Test-Path $path)) { continue }
    Write-Host "  exec $f"
    npx wrangler d1 execute $DbName --remote --file=$path 2>&1 | Out-Host
  }

  Write-Host "==> deploy worker"
  npx wrangler deploy 2>&1 | Out-Host

  $who2 = npx wrangler deployments list 2>&1 | Out-String
  Write-Host $who2

  # workers.dev subdomain guess
  $info = npx wrangler whoami 2>&1 | Out-String
  $apiGuess = "https://$WorkerName.<your-subdomain>.workers.dev"
  if ($info -match "workers\.dev") { }

  $secretFile = Join-Path $SecretsDir "daboo.cc.cd.json"
  $secretObj = @{
    domain = $Domain
    worker_name = $WorkerName
    d1_name = $DbName
    d1_id = $dbId
    admin_password = $AdminPass
    jwt_secret = $JwtSecret
    auth_mode = "x-admin-auth"
    api_base_workers_dev = "https://$WorkerName.<subdomain>.workers.dev"
    register_cpa_snippet = @{
      email_provider = "cloudflare"
      cloudflare_api_base = "https://$WorkerName.<subdomain>.workers.dev"
      cloudflare_api_key = $AdminPass
      cloudflare_auth_mode = "x-admin-auth"
      cloudflare_path_accounts = "/admin/new_address"
      cloudflare_path_messages = "/api/mails"
      cloudflare_path_token = "/api/token"
      cloudflare_path_domains = "/api/domains"
      defaultDomains = $Domain
    }
    next_manual_steps = @(
      "CF Dashboard -> daboo.cc.cd -> Email -> Email Routing -> Routing rules",
      "Catch-all: Send to a Worker -> select $WorkerName -> Save",
      "Optional: Workers -> $WorkerName -> Settings -> Domains -> add api.daboo.cc.cd",
      "Then set cloudflare_api_base to that URL in register-cpa/config.json"
    )
  }
  $secretObj | ConvertTo-Json -Depth 6 | Set-Content $secretFile -Encoding utf8
  Write-Host ""
  Write-Host "========================================"
  Write-Host "DEPLOY DONE (worker + D1)"
  Write-Host "Admin password: $AdminPass"
  Write-Host "Secrets saved: $secretFile"
  Write-Host "YOU MUST still bind Email Catch-all to worker in CF UI:"
  Write-Host "  Email -> Email Routing -> Catch-all -> Worker -> $WorkerName"
  Write-Host "========================================"
}
finally {
  Pop-Location
}
