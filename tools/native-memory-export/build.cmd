@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1
set "SOURCE_DIR=%~dp0"
set "OUTPUT_DIR=%~dp0..\..\build\plugin"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
pushd "%OUTPUT_DIR%"
ml64 /nologo /c /Foproxy_thunks.obj "%SOURCE_DIR%proxy.asm"
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /MT /EHsc /W4 /DNOMINMAX /LD /Fe:version.dll "%SOURCE_DIR%proxy.cpp" "%SOURCE_DIR%exporter.cpp" proxy_thunks.obj /link /DEF:"%SOURCE_DIR%version.def" /INCREMENTAL:NO bcrypt.lib
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /MT /EHsc /W4 /Fe:proxy-smoke.exe "%SOURCE_DIR%proxy-smoke.cpp"
if errorlevel 1 exit /b 1
popd
