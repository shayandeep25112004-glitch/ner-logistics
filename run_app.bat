@echo off
echo ======================================================================
echo  NER Logistics ^& Accessibility Intelligence Platform
echo ======================================================================
echo.

cd /d "%~dp0backend"
echo [1/2] Checking database and services...
python -c "import sqlite3; con=sqlite3.connect('../data/ner_platform.db'); print('Database ready with', con.execute('SELECT COUNT(*) FROM edge').fetchone()[0], 'road segments.')"

echo.
echo [2/2] Launching web server on http://localhost:8000 ...
echo Press Ctrl+C to stop.
echo.

start "" "http://localhost:8000"
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000
pause
