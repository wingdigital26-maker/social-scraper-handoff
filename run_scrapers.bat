@echo off
rem Scheduled entry point for the lead pipeline. Runs every active client
rem through collect -> load -> categorize -> draft, ending at drafts in the
rem OS inbox. Never sends anything.
rem
rem Logs to logs\pipeline-YYYY-MM-DD.log, appending, so one file per day and a
rem crashed run leaves evidence instead of vanishing.
cd /d C:\Users\wjack\social-scraper-handoff\ingest
for /f "tokens=1-3 delims=/- " %%a in ("%DATE%") do set D=%%c-%%a-%%b
echo. >> ..\logs\pipeline-%D%.log
echo ===== run started %DATE% %TIME% ===== >> ..\logs\pipeline-%D%.log
C:\Python314\python.exe run_pipeline.py --confirm >> ..\logs\pipeline-%D%.log 2>&1
echo ===== run finished %DATE% %TIME% exit=%ERRORLEVEL% ===== >> ..\logs\pipeline-%D%.log
