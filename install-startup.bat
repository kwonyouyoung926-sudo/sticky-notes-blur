@echo off
chcp 65001 >nul
rem 윈도우를 켤 때 StickyLock이 자동으로 실행되도록 시작프로그램 폴더에 바로가기를 만듭니다.
powershell -NoProfile -Command "$t=(Get-Command pythonw).Source; $s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\StickyLock.lnk'); $s.TargetPath=$t; $s.Arguments='\"%~dp0sticky_lock.pyw\"'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo 등록했습니다. 다음 부팅부터 자동으로 실행됩니다.
pause
