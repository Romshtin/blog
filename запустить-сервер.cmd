@echo off
rem Запуск локального сервера блога (API + сайт). Только ASCII — кириллица в .cmd запрещена.
cd /d "%~dp0"
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
pause