@echo off
chcp 65001 >nul
rem 윈도우를 켤 때 StickyLock이 자동으로 실행되도록 등록합니다.
rem exe가 있으면 exe를, 없으면 파이썬 버전을 등록합니다.
set "TARGET="
if exist "%~dp0StickyLock.exe" set "TARGET=%~dp0StickyLock.exe"
if not defined TARGET if exist "%~dp0dist\StickyLock.exe" set "TARGET=%~dp0dist\StickyLock.exe"

if defined TARGET (
  powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\StickyLock.lnk'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory=(Split-Path '%TARGET%'); $s.Save()"
  echo 등록했습니다 ^(exe^): %TARGET%
) else (
  powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\StickyLock.lnk'); $s.TargetPath=(Get-Command pythonw).Source; $s.Arguments='\"%~dp0sticky_lock.pyw\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"
  echo 등록했습니다 ^(파이썬^): %~dp0sticky_lock.pyw
)
echo 다음 부팅부터 자동으로 실행됩니다.
pause
