$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

python -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m compileall -q -f .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m pytest -q --cov=WriteRecovery --cov-branch --cov-report=term-missing --cov-fail-under=90
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

node --check www/lendingbot.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check www/v3-dashboard.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

git diff --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "MikaLendingBot verification passed."
