@echo off
rem Terminal wrapper for the Desktop Agent (used by ChatGPT via Remote Desktop Commander).
setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%HERE%pc_agent.py" %*
