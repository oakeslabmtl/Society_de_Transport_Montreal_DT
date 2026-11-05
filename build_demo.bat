@echo off
rem Builds the one-click replay demo: visualization\Build\STM-DT-Demo.zip (see build_demo.ps1).
rem Double-click it, or run ".\build_demo.bat --no-pause" from a terminal or script (never waits for a key).
rem ".\build_demo.bat --web" builds the web version instead, in visualization\Build\Web (with the standard
rem Godot 4.7, not .NET; to choose one: setx GODOT_WEB_BIN "<path to the console exe>").
rem Godot 4.7 .NET is found automatically. To choose one: setx GODOT_BIN "<path to the console exe>",
rem then open a new window (or double-click again).
setlocal DisableDelayedExpansion
set "SCRIPT_DIR=%~dp0"
set "PAUSE_AT_END="
set "WEB_ARG="

rem Double-clicked = cmd was started with this file on its command line (cmd /c ""...\build_demo.bat" ")
setlocal EnableDelayedExpansion
set "STARTED_FOR_ME="
set "CMD_LINE=!cmdcmdline!"
set "CMD_LINE_WITHOUT_ME=!CMD_LINE:%~nx0=!"
if not "!CMD_LINE_WITHOUT_ME!"=="!CMD_LINE!" set "STARTED_FOR_ME=1"
endlocal & set "PAUSE_AT_END=%STARTED_FOR_ME%"

:parse_args
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" set "PAUSE_AT_END="
if /i "%~1"=="--web" set "WEB_ARG=-Web"
shift
goto parse_args

:run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build_demo.ps1" -NoPause %WEB_ARG%
set "RESULT=%ERRORLEVEL%"
if defined PAUSE_AT_END (
    echo.
    pause
)
exit /b %RESULT%
