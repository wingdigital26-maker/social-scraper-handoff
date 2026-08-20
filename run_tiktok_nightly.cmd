@echo off
set ENV_FILE=C:\Users\wjack\ghl-cli\.env
cd /d %USERPROFILE%\social-scraper-handoff\ingest
python tiktok_ingest.py
python enrich.py
python db.py --source tiktok
