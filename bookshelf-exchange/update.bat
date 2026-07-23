@echo off
rem Fetch the latest version of the site. Your data (.env, bookshelf.db,
rem uploads) is untouched. Afterwards start the site with run.bat as usual.
setlocal
cd /d %~dp0
echo Downloading the latest version...
curl -fSL -o "%TEMP%\bookshelf_update.zip" "https://codeload.github.com/taiannangela/vllm/zip/refs/heads/claude/book-sharing-exchange-site-tkbgn3"
if errorlevel 1 goto fail
if exist "%TEMP%\bookshelf_update" rmdir /s /q "%TEMP%\bookshelf_update"
mkdir "%TEMP%\bookshelf_update"
tar -xf "%TEMP%\bookshelf_update.zip" -C "%TEMP%\bookshelf_update"
if errorlevel 1 goto fail
robocopy "%TEMP%\bookshelf_update\vllm-claude-book-sharing-exchange-site-tkbgn3\bookshelf-exchange" . /e /xf .env bookshelf.db secret_key /xd uploads .venv >nul
echo.
echo Update complete! Start the site with run.bat as usual.
goto end
:fail
echo.
echo Update failed - check your internet connection and try again.
:end
pause
