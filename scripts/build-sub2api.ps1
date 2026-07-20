# Build GrokPool custom Sub2API image and recreate the running stack.
# Usage (from repo root or anywhere):
#   powershell -ExecutionPolicy Bypass -File scripts\build-sub2api.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Sub2 = Join-Path $Root "sub2api"
$Deploy = Join-Path $Root "deploy"
$Image = "grokpool-sub2api:local"

if (-not (Test-Path (Join-Path $Sub2 "Dockerfile"))) {
    throw "sub2api Dockerfile not found: $Sub2"
}

Write-Host "==> Building $Image from $Sub2"
docker build -t $Image $Sub2
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

$envFile = Join-Path $Deploy ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "==> deploy/.env missing — copy deploy/.env.example to deploy/.env first"
    Write-Host "    Image built. Start stack later with: cd deploy; docker compose up -d"
    exit 0
}

Write-Host "==> Recreating stack with new image"
Push-Location $Deploy
try {
    docker compose --env-file .env up -d --force-recreate sub2api
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }
    docker compose ps
} finally {
    Pop-Location
}

Write-Host "==> Done. Admin UI: http://127.0.0.1:$((Select-String -Path $envFile -Pattern '^SERVER_PORT=' | ForEach-Object { ($_ -split '=',2)[1] } | Select-Object -First 1))"
