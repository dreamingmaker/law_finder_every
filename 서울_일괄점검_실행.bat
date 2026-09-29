@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 서울 일괄 점검 (조례정비 레이더)
echo 서울특별시·25개 자치구 자치법규에서 옛 법령명 인용을 점검합니다. (수십 분 걸릴 수 있습니다)
echo.
where python >/dev/null 2>&1 && (python radar_scan.py %*) || (py radar_scan.py %*)
echo.
echo 끝났습니다. 생성된 "실측결과_날짜" 폴더를 Claude에게 전달해 주세요.
pause
