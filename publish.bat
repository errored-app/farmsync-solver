@echo off
REM Rebuild the public branch from the current working tree and push it.
REM
REM The public repo carries one commit per release, not this repository's
REM development history: that history holds CLAUDE.md and data/, which name
REM real devices and accounts. `git push` publishes every reachable commit, so
REM untracking those files at the tip would not have been enough.
REM
REM Everything .gitignore excludes is excluded here too, which is what keeps
REM CLAUDE.md, data/, docs/, and scripts/ off GitHub.
REM
REM Usage: publish.bat 1.0.0
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Usage: publish.bat VERSION      e.g. publish.bat 1.0.0
    exit /b 1
)

for /f "delims=" %%v in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%v

REM A branch of public, publish-tmp, or HEAD (detached) means an earlier run
REM was interrupted and left us here. Continuing would capture one of those
REM as "the branch to return to", finish successfully, and strand the
REM operator there with nothing saying so.
if /i "%BRANCH%"=="public" (
    echo.
    echo Currently on branch "public" - this looks like a leftover from an
    echo interrupted publish. Check out your development branch first, e.g.
    echo "git checkout packaging", then run publish.bat again.
    exit /b 1
)
if /i "%BRANCH%"=="publish-tmp" (
    echo.
    echo Currently on branch "publish-tmp" - this looks like a leftover from
    echo an interrupted publish. Check out your development branch first,
    echo e.g. "git checkout packaging", then run publish.bat again.
    exit /b 1
)
if /i "%BRANCH%"=="HEAD" (
    echo.
    echo HEAD is detached - not on a real branch. Check out your development
    echo branch first, e.g. "git checkout packaging", then run publish.bat
    echo again.
    exit /b 1
)

REM origin gets force-pushed below. Refuse to run against any remote that
REM is not the public distribution repo, so a misconfigured origin cannot
REM overwrite the main branch of something else with real history.
for /f "delims=" %%u in ('git remote get-url origin') do set ORIGIN_URL=%%u
echo %ORIGIN_URL% | findstr /i "errored-app/farmsync-solver" >nul
if errorlevel 1 (
    echo.
    echo origin does not look like errored-app/farmsync-solver: %ORIGIN_URL%
    echo Refusing to force-push. Fix "origin" or edit publish.bat if this is
    echo intentional.
    exit /b 1
)

git diff --quiet && git diff --cached --quiet
if errorlevel 1 (
    echo Working tree is dirty. Commit or stash first.
    exit /b 1
)

git branch -D publish-tmp 2>nul
git checkout --orphan publish-tmp
if errorlevel 1 goto fail
git add -A
if errorlevel 1 goto fail
git commit -m "FarmsyncSolver %~1"
if errorlevel 1 goto fail

git branch -D public 2>nul
git branch -m public
if errorlevel 1 goto fail
git push -f origin public:main
if errorlevel 1 goto fail
git tag -f v%~1
git push -f origin v%~1
if errorlevel 1 goto fail

git checkout %BRANCH%
if errorlevel 1 goto fail
echo.
echo Published v%~1 to origin/main
exit /b 0

:fail
echo.
echo PUBLISH FAILED - returning to %BRANCH%
git checkout -f %BRANCH%
git branch -D publish-tmp 2>nul
exit /b 1
