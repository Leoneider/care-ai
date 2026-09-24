@echo off
REM Envoltorio de scripts\care.ps1.
REM
REM Existe por el ExecutionPolicy de PowerShell: en una maquina recien montada,
REM un .ps1 descargado o creado por otra herramienta no corre, y el error que da
REM no dice como arreglarlo. El -ExecutionPolicy Bypass aplica solo a esta
REM invocacion; no cambia nada de la configuracion del equipo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\care.ps1" %*
