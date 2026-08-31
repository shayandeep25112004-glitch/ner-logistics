#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR/backend"

echo "======================================================================"
echo " NER Logistics & Accessibility Intelligence Platform"
echo "======================================================================"
echo ""
echo "[1/2] Checking database..."
python3 -c "import sqlite3; con=sqlite3.connect('../data/ner_platform.db'); print('Database ready with', con.execute('SELECT COUNT(*) FROM edge').fetchone()[0], 'road segments.')"

echo ""
echo "[2/2] Launching web server at http://localhost:8000 ..."
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
