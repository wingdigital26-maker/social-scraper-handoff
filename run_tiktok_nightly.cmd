@echo off
REM TikTok nightly. Exits NON-ZERO when the run finds nothing, so Task Scheduler
REM shows red instead of a green checkmark. Four consecutive runs reported
REM found=0 / inserted=0 while exiting 0, which is the most dangerous state in
REM the whole system: healthy-looking and producing nothing.
set ENV_FILE=C:\Users\wjack\ghl-cli\.env
cd /d %USERPROFILE%\social-scraper-handoff\ingest

python tiktok_ingest.py > "%TEMP%\tiktok_run.log" 2>&1
type "%TEMP%\tiktok_run.log"

REM "kept : 0" in the run summary means discovery produced nothing usable.
findstr /C:"kept        : 0" "%TEMP%\tiktok_run.log" >nul
if %ERRORLEVEL%==0 (
  echo TIKTOK YIELD ZERO - failing the task so this surfaces instead of reading as success.
  exit /b 2
)

python enrich.py
python db.py --source tiktok
