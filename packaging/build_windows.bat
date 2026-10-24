@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo ============================================
echo  华为手表传歌 - Windows 打包
echo ============================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python 启动器 py。请先安装 Python 3.10+ 并勾选 "Add to PATH"。
    pause
    exit /b 1
)

if not exist .venv (
    echo [1/5] 创建虚拟环境 .venv ...
    py -3 -m venv .venv || goto :fail
) else (
    echo [1/5] 使用已有虚拟环境 .venv
)

echo [2/5] 安装依赖 ...
call .venv\Scripts\python.exe -m pip install --upgrade pip >nul
call .venv\Scripts\python.exe -m pip install -e ".[gui,build,dev]" || goto :fail

echo [3/5] 生成图标 ...
call .venv\Scripts\python.exe packaging\make_icon.py || goto :fail

echo [4/5] 运行测试 ...
call .venv\Scripts\python.exe -m pytest -q || goto :fail

echo [5/5] 打包单文件 exe ...
call .venv\Scripts\pyinstaller.exe packaging\hwmusic.spec --noconfirm --clean || goto :fail

echo.
echo 完成: dist\HuaweiMusic.exe
echo 把这个 exe 复制到任何 Windows 10/11 电脑即可双击使用。
pause
exit /b 0

:fail
echo.
echo [失败] 上一步出错，请查看上面的日志。
pause
exit /b 1
