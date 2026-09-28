@echo off
rem Download / complete the models (about 2.5 GB, only needed once). Double-click to run.
rem Same as: start_server.exe --download-models  (source and SHA-256 list: core\server\model_download.py)
"%~dp0start_server.exe" --download-models %*
set "code=%errorlevel%"
pause
exit /b %code%
