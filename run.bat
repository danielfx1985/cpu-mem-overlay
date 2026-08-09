@echo off
cd /d "%~dp0"
:: 通过 VBS 隐藏控制台启动；若要用命令行调试请直接: python main.py
wscript.exe "%~dp0run.vbs"
