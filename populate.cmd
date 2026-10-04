@echo off
REM Populate an OpenCloud instance with the Weyland demo data (see README.md).
cd /d "%~dp0"
python populate.py %*
