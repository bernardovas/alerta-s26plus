#Requires -Version 5.1
# Instalador do Alerta S26+ (Windows)
# - instala o Python, se faltar
# - instala as bibliotecas numa pasta isolada (.venv)
# - guarda o token e o chat do Telegram em segredos.env (só nesta pasta)
# - agenda a verificação a cada 1 hora no Agendador de Tarefas
# - roda um teste e manda um resumo no Telegram

$ErrorActionPreference = "Stop"
$Pasta = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Pasta
$NomeTarefa = "Alerta S26 Plus"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Titulo($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

function Achar-Python {
    $candidatos = @()
    foreach ($cmd in @("py", "python")) {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike "*WindowsApps*") { $candidatos += $c.Source }
    }
    $candidatos += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    $candidatos += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($p in $candidatos) {
        try {
            $args3 = @(); if ($p -like "*\py.exe") { $args3 = @("-3") }
            $v = & $p @args3 -c "import sys; print(sys.version_info >= (3, 9))" 2>$null
            if ($v -eq "True") { return @{ Exe = $p; Args = $args3 } }
        } catch { }
    }
    return $null
}

try {
    Write-Host "Alerta de preço - Samsung Galaxy S26+" -ForegroundColor Green
    Write-Host "Pasta: $Pasta"

    # 1) Python ----------------------------------------------------------------
    Titulo "Verificando o Python"
    $py = Achar-Python
    if (-not $py) {
        Write-Host "Python não encontrado. Instalando o Python 3.12 (pode levar alguns minutos)..."
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $py = Achar-Python
        if (-not $py) {
            throw "Não consegui instalar o Python automaticamente. Instale pelo site python.org (marque 'Add python.exe to PATH') e rode o instalar.bat de novo."
        }
    }
    Write-Host "Usando: $($py.Exe)"

    # 2) Ambiente isolado + bibliotecas ---------------------------------------
    Titulo "Instalando as bibliotecas"
    $venvPy = Join-Path $Pasta ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        & $py.Exe @($py.Args) -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Falha ao criar o ambiente Python (.venv)." }
    }
    & $venvPy -m pip install --disable-pip-version-check -q --upgrade pip
    & $venvPy -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as bibliotecas." }
    Write-Host "OK"

    # 3) Telegram --------------------------------------------------------------
    Titulo "Configurando o Telegram"
    $arqSegredos = Join-Path $Pasta "segredos.env"
    $pedir = $true
    if (Test-Path $arqSegredos) {
        $r = Read-Host "Já existe um token salvo. Quer trocar? (s/N)"
        $pedir = ($r -match '^[sS]')
    }
    if ($pedir) {
        Write-Host "Cole o TOKEN do bot (aquele que o @BotFather enviou). Ele não aparece na tela ao colar."
        $seguro = Read-Host "Token" -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro)).Trim()
        $chat = (Read-Host "Seu ID do Telegram (o número do @userinfobot)").Trim()
        if ($token -notmatch '^\d+:[\w-]{20,}$') { throw "O token parece incompleto. Copie de novo a mensagem do @BotFather e rode o instalar.bat outra vez." }
        if ($chat -notmatch '^-?\d+$') { throw "O ID deve ser só números. Confira no @userinfobot." }
        $conteudo = "TELEGRAM_BOT_TOKEN=$token`r`nTELEGRAM_CHAT_ID=$chat`r`n"
        [IO.File]::WriteAllText($arqSegredos, $conteudo, (New-Object Text.UTF8Encoding($false)))
        # Deixa o arquivo legível só pelo seu usuário do Windows.
        icacls $arqSegredos /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
        Write-Host "Salvo em segredos.env (acessível só pelo seu usuário)."
    }

    # 4) Agendamento -------------------------------------------------------------
    $Horas = 2
    try { $Horas = [int]((Get-Content (Join-Path $Pasta "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json).intervalo_horas) } catch { }
    if ($Horas -lt 1) { $Horas = 2 }
    Titulo "Agendando a verificação a cada $Horas hora(s)"
    $pyw = Join-Path $Pasta ".venv\Scripts\pythonw.exe"
    $acao = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Pasta\executar.pyw`"" -WorkingDirectory $Pasta
    $gatilho = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Hours $Horas)
    $config = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho -Settings $config `
        -Description "Verifica o preço do Galaxy S26+ e avisa no Telegram." -Force | Out-Null
    Write-Host "Tarefa '$NomeTarefa' criada. Ela roda enquanto você estiver logado no Windows,"
    Write-Host "e se o PC estiver desligado na hora marcada, roda assim que ele ligar."

    # 5) Teste -------------------------------------------------------------------
    Titulo "Rodando um teste agora (cerca de 1 a 2 minutos)"
    $env:TEST_MODE = "1"
    & $venvPy monitor.py
    Remove-Item Env:TEST_MODE

    Write-Host ""
    Write-Host "Pronto! Confira a mensagem de teste no seu Telegram." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "ERRO: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ""
Read-Host "Pressione Enter para fechar"
