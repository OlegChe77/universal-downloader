# Запуск Универсального скачивателя: ставит всё недостающее и запускает программу.
# Вызывается из Запуск.bat (с окном) и из launch.vbs с --hidden (без окна: ход установки — значок у часов,
# ошибка — окно сообщения). Файл должен быть в UTF-8 с BOM, иначе Windows PowerShell 5.1 испортит русский текст.

# Язык сообщений — как у программы: «lang» в settings.json, пусто — язык Windows (как в i18n.py)
$Lang = ''
try { $Lang = (Get-Content (Join-Path $PSScriptRoot 'settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json).lang } catch {}
if ($Lang -ne 'ru' -and $Lang -ne 'en') {
    $Lang = if ((Get-UICulture).TwoLetterISOLanguageName -in 'ru', 'uk', 'be', 'kk', 'ky', 'uz', 'tg') { 'ru' } else { 'en' }
}
function T($ru, $en) { if ($Lang -eq 'en') { $en } else { $ru } }
$AppName = T 'Универсальный скачиватель' 'Universal Downloader'
$Host.UI.RawUI.WindowTitle = $AppName
$ProgressPreference = 'SilentlyContinue'  # без этого Invoke-WebRequest качает в разы медленнее
Set-Location $PSScriptRoot
$Hidden = $args -contains '--hidden'
$AppArgs = @($args | Where-Object { $_ -ne '--hidden' })

# Свой Python лежит в папке программы (python\): встроенный (embeddable) Python с python.org,
# ставить Python в систему не нужно. Хэши сверены с python.org (MD5) и PyPI (SHA-256).
$PyVersion = '3.12.10'
$PyZipUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$PyZipSha256 = '4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3'
$PipWheel = 'pip-26.2.1-py3-none-any.whl'
$PipUrl = "https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/$PipWheel"
$PipSha256 = '71138ADF1F4CA900CDB7D289C21B7494329F2332B6D85F0E1C42108C0384ED3E'
$PyDir = Join-Path $PSScriptRoot 'python'
$Python = Join-Path $PyDir 'python.exe'

function Fail($message) {
    if ($Hidden) {
        if ($Tray) { $Tray.Dispose() }
        [System.Windows.Forms.MessageBox]::Show($message, $AppName, 'OK', 'Error') | Out-Null
        exit 1
    }
    Write-Host ''
    Write-Host $message -ForegroundColor Red
    Read-Host (T 'Нажмите Enter, чтобы закрыть окно' 'Press Enter to close the window')
    exit 1
}

function New-DesktopShortcut {
    # Ярлык запускает launch.vbs: он открывает программу без чёрного окна
    $desktop = [Environment]::GetFolderPath('Desktop')
    # ярлык с любым из двух имён уже есть — второй не нужен (Удалить.bat ищет оба)
    if ((Test-Path (Join-Path $desktop 'Универсальный скачиватель.lnk')) -or (Test-Path (Join-Path $desktop 'Universal Downloader.lnk'))) { return }
    $link = Join-Path $desktop "$AppName.lnk"
    try {
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
        $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
        $shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'launch.vbs') + '"'
        $shortcut.WorkingDirectory = $PSScriptRoot
        $shortcut.IconLocation = (Join-Path $PSScriptRoot 'web\icon.ico') + ',0'
        $shortcut.Description = T 'Скачать видео и музыку по ссылке' 'Download video and music by link'
        $shortcut.Save()
        Write-Host (T "На рабочем столе появился ярлык «$AppName»." "The “$AppName” shortcut is now on the desktop.")
    } catch {
        Write-Host ((T 'Не удалось создать ярлык: ' 'Could not create the shortcut: ') + $_.Exception.Message)
    }
}

function Test-Python($exe) {
    if (-not (Test-Path $exe)) { return $false }
    try {
        & $exe -c 'import sys, pip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-Verified($url, $file, $sha256) {
    # Скачанное, у которого не совпал хэш, не используем
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $file
    $hash = (Get-FileHash -Algorithm SHA256 $file).Hash
    if ($hash -ne $sha256) { throw "у файла $(Split-Path $file -Leaf) не тот хэш ($hash) - не использую" }
}

function Install-Python {
    Write-Host (T "Скачиваю Python $PyVersion с python.org в папку программы..." "Downloading Python $PyVersion from python.org into the program folder...")
    $temp = Join-Path $env:TEMP ('udl-python-' + [guid]::NewGuid())
    $stage = "$PyDir.new"  # собираем рядом и потом переименовываем: недоделанной папки python не будет
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        New-Item -ItemType Directory $temp | Out-Null
        $zip = Join-Path $temp 'python.zip'
        $wheel = Join-Path $temp $PipWheel
        Get-Verified $PyZipUrl $zip $PyZipSha256
        Get-Verified $PipUrl $wheel $PipSha256
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
        Expand-Archive $zip $stage
        # И подпись самого Python: Python Software Foundation
        foreach ($name in 'python.exe', 'python312.dll') {
            $sig = Get-AuthenticodeSignature (Join-Path $stage $name)
            if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Python Software Foundation') {
                throw "$name не прошёл проверку подписи ($($sig.Status))"
            }
        }
        # Пути встроенного Python: стандартная библиотека, Lib\site-packages (через site) и папка программы
        # (иначе не найдётся sites.py). Переменные PYTHON* и пакеты из системы он не видит.
        Set-Content -Encoding ASCII (Join-Path $stage 'python312._pth') @('python312.zip', '.', '..', 'import site')
        # pip: wheel — это zip, распаковываем в site-packages, а потом pip ставит сам себя как положено
        Write-Host (T 'Ставлю pip...' 'Installing pip...')
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($wheel, (Join-Path $stage 'Lib\site-packages'))
        & (Join-Path $stage 'python.exe') -m pip install -q --no-index --force-reinstall --no-warn-script-location --disable-pip-version-check $wheel
        if ($LASTEXITCODE -ne 0) { throw 'pip не установился' }
        if (Test-Path $PyDir) { Remove-Item $PyDir -Recurse -Force }
        Rename-Item $stage (Split-Path $PyDir -Leaf)
        return $true
    } catch {
        Write-Host ((T 'Не удалось установить Python: ' 'Could not install Python: ') + $_.Exception.Message)
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue }
        return $false
    } finally {
        Remove-Item $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Ярлык нажали ещё раз, пока идёт установка, — вторая не нужна: программу запустит первая
$Mutex = [System.Threading.Mutex]::new($false, 'Local\udl-start-' + ($PSScriptRoot -replace '[\\:]', '_'))
try { $owned = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) {
    if (-not $Hidden) { Write-Host (T 'Установка уже идёт - программа запустится сама.' 'Installation is already running - the program will start by itself.'); Start-Sleep -Seconds 4 }
    exit 0
}

$Tray = $null
if ($Hidden) {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $Tray = New-Object System.Windows.Forms.NotifyIcon
    $Tray.Icon = New-Object System.Drawing.Icon (Join-Path $PSScriptRoot 'web\icon.ico')
    $Tray.Text = $AppName + (T ': установка...' ': installing...')
    $Tray.Visible = $true
    $Tray.ShowBalloonTip(15000, $AppName,
        (T 'Устанавливаю нужные компоненты — это займёт пару минут. Страница откроется сама.' 'Installing components — this takes a couple of minutes. The page opens by itself.'), 'Info')
}

if (-not (Test-Python $Python)) {
    if (Test-Path $PyDir) { Write-Host (T 'Python программы повреждён, ставлю заново...' 'The program Python is damaged, reinstalling...') }
    else { Write-Host (T 'Первый запуск: устанавливаю всё нужное, это займёт пару минут...' 'First start: installing everything needed, this takes a couple of minutes...') }
    if (-not (Install-Python) -or -not (Test-Python $Python)) {
        Fail (T 'Не удалось установить Python. Проверьте интернет и запустите снова.' 'Could not install Python. Check the internet and start again.')
    }
}

# Ставим недостающее и обновляем yt-dlp: сайты часто меняют защиту
Write-Host (T 'Проверяю компоненты...' 'Checking components...')
& $Python -m pip install -q --disable-pip-version-check --no-warn-script-location -U -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    & $Python -c 'import yt_dlp, flask, imageio_ffmpeg' 2>$null
    if ($LASTEXITCODE -ne 0) { Fail (T 'Не удалось установить компоненты. Проверьте интернет и запустите снова.' 'Could not install components. Check the internet and start again.') }
    Write-Host (T 'Не удалось обновить компоненты - запускаю с теми, что есть.' 'Could not update components - starting with what is there.')
} else {
    # По этой копии launch.vbs понимает, что всё установлено и окно больше не нужно
    Copy-Item requirements.txt (Join-Path $PyDir 'requirements.installed')
    New-Item -ItemType File -Force (Join-Path $PyDir 'ytdlp-updated') | Out-Null
}

New-DesktopShortcut

# Программа работает без окна: значок у часов, меню по правому щелчку
Start-Process -FilePath $Python -ArgumentList (@('app.py', '--tray') + $AppArgs) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
if ($Hidden) { $Tray.Dispose(); exit 0 }
Write-Host ''
Write-Host (T 'Готово! Программа работает в фоне: значок со стрелкой у часов (справа внизу).' 'Done! The program runs in the background: the arrow icon by the clock (bottom right).') -ForegroundColor Green
Write-Host (T 'Это окно закроется само.' 'This window closes by itself.')
Start-Sleep -Seconds 4
