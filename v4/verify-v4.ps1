$ErrorActionPreference = "Stop"
$V4Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepositoryRoot = Split-Path -Parent $V4Root
Push-Location -LiteralPath $V4Root
try {
    python -m ruff check .
    python -m compileall -q .
    node --check www/app.js
    python -m pytest -q --cov=mika_v4 --cov-report=term --cov-fail-under=85
    python -m pytest -q `
        --cov=mika_v4.market `
        --cov=mika_v4.strategy `
        --cov=mika_v4.execution `
        --cov=mika_v4.store `
        --cov-report=term `
        --cov-fail-under=90
} finally {
    Pop-Location
}
Push-Location -LiteralPath $RepositoryRoot
try {
    git diff --check
} finally {
    Pop-Location
}
