# PowerShell Runner for NER Logistics Platform
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " NER Logistics & Accessibility Intelligence Platform" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

$BackendPath = Join-Path $PSScriptRoot "backend"
Set-Location $BackendPath

Write-Host "[1/2] Checking database..." -ForegroundColor Yellow
python -c "import sqlite3; con=sqlite3.connect('../data/ner_platform.db'); print('Database ready with', con.execute('SELECT COUNT(*) FROM edge').fetchone()[0], 'road segments.')"

Write-Host ""
Write-Host "[2/2] Launching web server at http://localhost:8000 ..." -ForegroundColor Green
Start-Process "http://localhost:8000"

python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
