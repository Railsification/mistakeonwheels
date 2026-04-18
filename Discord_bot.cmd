@echo off
title HotBot - One Click Launcher (Reloadable + Logs)
setlocal

REM === SETTINGS ===
set "BOTDIR=C:\Users\djlan\mistakesonwheel-bot"
set "SCRIPT=hotbot.py"
set "VENV_ACT=%BOTDIR%\.venv\Scripts\activate.bat"

REM Logging
set "LOGDIR=%BOTDIR%\logs"
set "LOGFILE=%LOGDIR%\hotbot.log"

REM Single-instance lock (directory-based)
set "LOCKDIR=%BOTDIR%\hotbot.lockdir"
REM ===============

if not exist "%LOGDIR%" (
    mkdir "%LOGDIR%" >nul 2>&1
)

REM ==========================================================
REM SINGLE INSTANCE GUARD (launcher-level, no powershell)
REM ==========================================================
mkdir "%LOCKDIR%" >nul 2>&1
if errorlevel 1 (
    echo =======================================
    echo HotBot launcher is ALREADY RUNNING.
    echo Lock: %LOCKDIR%
    echo =======================================
    echo.
    echo If you are sure it's not running, delete:
    echo   %LOCKDIR%
    echo.
    echo Press any key to exit...
    pause >nul
    exit /b 0
)

echo [%date% %time%] [INFO] Launcher started (lock acquired) >> "%LOGFILE%"

:MAIN_MENU
cls
echo =======================================
echo          HotBot - Launcher
echo =======================================
echo Bot dir : %BOTDIR%
echo Script  : %SCRIPT%
echo Log file: %LOGFILE%
echo ---------------------------------------
echo.
echo [S] Start / restart bot
echo [V] View log
echo [Q] Quit
echo.

choice /C SVQ /N /M "Select option [S/V/Q]: "
if errorlevel 3 goto MENU_QUIT
if errorlevel 2 goto VIEW_LOG
if errorlevel 1 goto RUN_BOT

goto MAIN_MENU


:RUN_BOT
cls
echo ---------------------------------------
echo  Starting HotBot
echo ---------------------------------------
echo.

cd /d "%BOTDIR%"

REM Activate virtual env
if exist "%VENV_ACT%" (
    echo [%date% %time%] [INFO] Activating venv >> "%LOGFILE%"
    call "%VENV_ACT%"
) else (
    echo [ERR] Could not find venv at:
    echo       "%VENV_ACT%"
    echo [%date% %time%] [ERROR] Missing venv at "%VENV_ACT%" >> "%LOGFILE%"
    echo.
    echo Press any key to return to menu...
    pause >nul
    goto MAIN_MENU
)

echo [%date% %time%] [INFO] ----- HotBot START ----- >> "%LOGFILE%"

:BOT_LOOP
cls
echo ---------------------------------------
echo HotBot is running...
echo ---------------------------------------
echo.
echo To stop the bot, press Ctrl+C in this window.
echo The launcher will then let you restart or quit.
echo.

echo [%date% %time%] [INFO] Launching python "%SCRIPT%" >> "%LOGFILE%"

REM Run the bot (live output in this window, no redirection)
python "%SCRIPT%"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ---------------------------------------
echo HotBot process ended.
echo Exit code: %EXITCODE%
echo ---------------------------------------
echo [%date% %time%] [INFO] HotBot exited with code %EXITCODE% >> "%LOGFILE%"

echo.
echo What would you like to do?
echo   [R] Restart bot
echo   [M] Return to main menu
echo   [Q] Quit launcher
echo.

choice /C RMQ /N /M "Select option [R/M/Q]: "
if errorlevel 3 goto MENU_QUIT
if errorlevel 2 goto MAIN_MENU
if errorlevel 1 goto BOT_LOOP

goto MAIN_MENU


:VIEW_LOG
cls
echo =======================================
echo              HotBot Log
echo =======================================
echo File: %LOGFILE%
echo.

if exist "%LOGFILE%" (
    type "%LOGFILE%"
) else (
    echo No log file yet. Run the bot at least once.
)

echo.
echo ---------------------------------------
echo Press any key to return to menu...
pause >nul
goto MAIN_MENU


:MENU_QUIT
echo.
echo [%date% %time%] [INFO] Launcher exiting >> "%LOGFILE%"
echo Exiting launcher...

REM Release lock
rmdir "%LOCKDIR%" >nul 2>&1

exit /b
