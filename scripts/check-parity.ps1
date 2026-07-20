# Verify running Docker binary contains GrokPool critical features.
$ErrorActionPreference = "Stop"

function Has-String([string]$s) {
    docker exec sub2api sh -c "grep -aF '$s' /app/sub2api >/dev/null && echo yes || echo no"
}

Write-Host "Container:"
docker ps --filter "name=^sub2api$" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

$checks = @(
    "probe-dead",
    "ClassifyGrokAccountLiveness",
    "sso-to-oauth",
    "missing referrer=grok-build",
    "authorization_code",
    "cli-chat-proxy.grok.com",
    "grok-build"
)

$bad = 0
foreach ($c in $checks) {
    $r = (Has-String $c).Trim()
    if ($r -eq "yes") {
        Write-Host "[OK] $c"
    } else {
        Write-Host "[MISS] $c"
        $bad++
    }
}

# Route shape (401 = exists+auth required; 404 = missing)
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:18080/api/v1/admin/grok/accounts/probe-dead" -Method POST -ContentType "application/json" -Body "{}" -UseBasicParsing -TimeoutSec 5 | Out-Null
    Write-Host "[WARN] probe-dead returned 200 without auth?"
} catch {
    $code = 0
    try { $code = [int]$_.Exception.Response.StatusCode } catch {}
    if ($code -eq 401 -or $code -eq 403) { Write-Host "[OK] probe-dead route present (HTTP $code)" }
    elseif ($code -eq 404) { Write-Host "[MISS] probe-dead route 404"; $bad++ }
    else { Write-Host "[WARN] probe-dead HTTP $code / $($_.Exception.Message)" }
}

if ($bad -gt 0) {
    Write-Host "FAIL: $bad checks missing — rebuild: scripts\build-sub2api.ps1"
    exit 1
}
Write-Host "PASS: running Docker matches GrokPool feature set"
