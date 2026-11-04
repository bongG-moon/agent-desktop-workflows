@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 goto launcher
where python.exe >nul 2>nul
if errorlevel 1 goto missing
python.exe -X utf8 -c "import sys; assert sys.version_info >= (3,10); import tkinter" >nul 2>nul
if errorlevel 1 goto missing
where pythonw.exe >nul 2>nul
if errorlevel 1 goto consolepython
start "" /b pythonw.exe -X utf8 -B "%~dp0scripts\cli.py" ui
exit /b 0
:consolepython
python.exe -X utf8 -B "%~dp0scripts\cli.py" ui
if errorlevel 1 pause
exit /b
:launcher
py.exe -3 -X utf8 -c "import sys; assert sys.version_info >= (3,10); import tkinter" >nul 2>nul
if errorlevel 1 goto missing
where pyw.exe >nul 2>nul
if errorlevel 1 goto consolelauncher
start "" /b pyw.exe -3 -X utf8 -B "%~dp0scripts\cli.py" ui
exit /b 0
:consolelauncher
py.exe -3 -X utf8 -B "%~dp0scripts\cli.py" ui
if errorlevel 1 pause
exit /b
:missing
echo Python 3.10 이상과 tkinter가 필요합니다.
echo 회사에서 승인한 Python 설치를 확인하고 사용가이드.html을 열어 주세요.
echo 기존 CUA, 모델 설정, 보안 정책은 이 실행 파일이 변경하지 않습니다.
pause
exit /b 2
