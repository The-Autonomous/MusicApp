@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist temp mkdir temp
set "TEMP=%~dp0temp"
set "TMP=%~dp0temp"
set "MSYS2_PATH_TYPE=inherit"
set "FFMPEG_VARIANT=%~1"
if "%FFMPEG_VARIANT%"=="" set "FFMPEG_VARIANT=msvc"
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b 1
set "PATH=%~dp0build-tools\nasm-2.16.03;%~dp0build-tools\usr\bin;%PATH%;C:\msys64\usr\bin;C:\Program Files\Git\usr\bin"
if "%FFMPEG_VARIANT:~0,5%"=="clang" set "PATH=C:\Program Files\LLVM\bin;%PATH%"
set "INCLUDE=%~dp0prefix\include;%INCLUDE%"
set "LIB=%~dp0prefix\lib;%LIB%"
if "%FFMPEG_VARIANT%"=="clang-pgo-train" set "LIB=C:\Program Files\LLVM\lib\clang\20\lib\windows;%LIB%"
if not exist prefix\include\lame mkdir prefix\include\lame
if not exist prefix\lib mkdir prefix\lib
pushd sources\lame-3.100
nmake /nologo /f Makefile.MSVC MSVCVER=Win64 ASM=NO MACHINE=/machine:X64 "CC_OPTS=/nologo /DWin64 /O2 /Ob2 /Oi /GL /GS /fp:precise /MT /DFLOAT8=float /DREAL_IS_FLOAT=1" config.h libmp3lame-static.lib
if errorlevel 1 exit /b 1
copy /y output\libmp3lame-static.lib ..\..\prefix\lib\mp3lame.lib
copy /y include\lame.h ..\..\prefix\include\lame\lame.h
popd
set "CMAKE=C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
"%CMAKE%" -S sources/zlib-1.3.2 -B build/zlib -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release -DZLIB_BUILD_SHARED=OFF -DZLIB_BUILD_STATIC=ON -DZLIB_BUILD_TESTING=OFF -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded -DCMAKE_POLICY_DEFAULT_CMP0091=NEW -DCMAKE_INSTALL_PREFIX="%~dp0prefix"
if errorlevel 1 exit /b 1
"%CMAKE%" --build build/zlib --target install
if errorlevel 1 exit /b 1
copy /y prefix\lib\zs.lib prefix\lib\zlib.lib
if not exist prefix\lib\zlib.lib exit /b 1
C:\msys64\usr\bin\bash.exe --noprofile --norc build-slim.sh
exit /b %errorlevel%
