@echo off
rem StickyLock 실행 (창 없이 백그라운드로)
start "" pythonw "%~dp0sticky_lock.pyw" || start "" pyw "%~dp0sticky_lock.pyw"
