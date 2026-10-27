@echo off
REM Atalho Windows portatil (%%~dp0 = diretorio deste .bat)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Criando venv e instalando dependencias ^(pode demorar varios minutos^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERRO: Python nao encontrado no PATH.
        echo Instale de https://www.python.org/downloads/windows/
        echo e marque a opcao "Add python.exe to PATH".
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERRO ao instalar dependencias. Veja as mensagens acima.
        pause
        exit /b 1
    )
) else (
    echo [1/2] venv ja existe, reutilizando.
)

echo [2/2] Iniciando servidor em http://localhost:8080
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8080
cmd /k

pause