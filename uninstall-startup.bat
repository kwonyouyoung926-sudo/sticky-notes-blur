@echo off
chcp 65001 >nul
rem 자동 실행 등록을 해제합니다. (프로그램 자체는 그대로 남습니다)
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup')+'\StickyLock.lnk') -ErrorAction SilentlyContinue"
echo 해제했습니다.
pause
