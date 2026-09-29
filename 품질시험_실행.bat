@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >/dev/null 2>&1 && (python -m unittest discover -s tests -v) || (py -m unittest discover -s tests -v)
pause
