# Promote clean work from branch `local` -> `main` and push public release.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1
# Does NOT commit secrets. Run from repo root after you're happy on `local`.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "local") {
  Write-Host "Current branch is '$branch'. Switch to local first: git checkout local"
  exit 1
}

# block secrets in working tree before merge
$danger = @(
  "deploy/.env",
  "register-win/config.json",
  "register-win/token.json",
  "register-win/mail_credentials.txt"
) | Where-Object { Test-Path $_ }

Write-Host "==> working on local; personal runtime files may exist (gitignored):"
$danger | ForEach-Object { "  - $_" }

# ensure ignored
git status --porcelain | Select-String -Pattern "\.env$|token\.json|config\.json|mail_credentials|accounts_" | ForEach-Object {
  throw "Refusing promote: tracked/staged secret-looking path: $_"
}

Write-Host "==> fetch origin"
git fetch origin

Write-Host "==> checkout main and merge local"
git checkout main
git pull --ff-only origin main
git merge local --no-ff -m "release: merge local into main"

# final secret scan on merge result index
$bad = git diff --name-only origin/main...HEAD | Select-String -Pattern "(^|/)\.env$|token\.json|mail_credentials|register-win/config\.json|accounts_.*\.txt|\.git\.bak"
if ($bad) {
  git checkout local
  throw "Abort push, secret-looking files in release diff: $bad"
}

Write-Host "==> push main"
git push origin main

Write-Host "==> back to local branch for daily work"
git checkout local
Write-Host "Done. Public: https://github.com/xubin0903/GrokPool"
