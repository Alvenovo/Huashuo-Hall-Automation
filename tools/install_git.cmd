@echo off
REM ---------------------------------------------------------------------------
REM install_git.ps1 wrapper -- an entry point that does NOT depend on the
REM PowerShell execution policy.
REM
REM Why this file exists (2026-09-23, first real new test machine):
REM   A fresh Windows client ships with ExecutionPolicy = Restricted, and a UNC
REM   path (\\host\share\...) is treated as "remote" even under RemoteSigned.
REM   Calling the .ps1 directly therefore dies with a SecurityError:
REM       <file> cannot be loaded because running scripts is disabled on this
REM       system.  CategoryInfo: SecurityError, UnauthorizedAccess
REM   The execution policy applies to script FILES only -- a .cmd is not a script
REM   file, so this wrapper always gets through, then hands over with -Bypass.
REM   (Full Chinese explanation: header of install_git.ps1.)
REM
REM Usage (new test machine; plain PowerShell window or cmd window both work):
REM   \\LAPTOP-VS5F7HF4\hall-packages\repo\install_git.cmd
REM   \\LAPTOP-VS5F7HF4\hall-packages\repo\install_git.cmd -Force   (reinstall)
REM
REM Keep this file ASCII-only and CRLF-terminated: cmd.exe reads .cmd with the
REM OEM code page, so Chinese text here would come out garbled.  (That is a
REM different rule from install_git.ps1, which needs UTF-8 WITH BOM.)
REM ---------------------------------------------------------------------------

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_git.ps1" %*
exit /b %ERRORLEVEL%
