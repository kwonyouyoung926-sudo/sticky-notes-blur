@echo off
chcp 65001 >nul
rem 파이썬 없이도 쓸 수 있는 StickyLock.exe 를 만듭니다. (결과: dist\StickyLock.exe)
cd /d "%~dp0"
python -m pip install --quiet pyinstaller || goto :error
copy /y sticky_lock.pyw sticky_lock_build.py >nul
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name StickyLock sticky_lock_build.py || goto :error
del sticky_lock_build.py
echo.
echo 완료: dist\StickyLock.exe
pause
exit /b

:error
echo.
echo 실패했습니다. 파이썬이 설치되어 있는지 확인하세요.
pause
