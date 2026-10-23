# Удаление Универсального скачивателя: закрывает программу, убирает ярлык, ссылку udl-downloader: и папку программы.
# Вызывается из Удалить.bat. Файл должен быть в UTF-8 с BOM, иначе Windows PowerShell 5.1 испортит русский текст.

$Host.UI.RawUI.WindowTitle = 'Удаление: Универсальный скачиватель'
$Dir = $PSScriptRoot
Set-Location $env:TEMP  # из своей папки — иначе она занята и не удалится

function Done($message, $color = 'Gray') {
    Write-Host ''
    if ($message) { Write-Host $message -ForegroundColor $color }
    Read-Host 'Нажмите Enter, чтобы закрыть окно'
    exit
}

function Ask($question) {
    # «Д» в английской раскладке — «L»
    return (Read-Host "$question (Д/Н)").Trim() -match '^(д|да|l|y|yes)$'
}

function Test-Ours($text) {
    # Ярлык и ссылка могли остаться от другой копии программы — их не трогаем
    return $text -and $text.IndexOf("$Dir\launch.vbs", [StringComparison]::OrdinalIgnoreCase) -ge 0
}

if (-not (Test-Path "$Dir\app.py") -or -not (Test-Path "$Dir\launch.vbs")) { Done 'Это не папка программы — ничего не удаляю.' Red }
if (Test-Path "$Dir\.git") { Done 'Это папка разработки (в ней .git) — не удаляю.' Red }

Write-Host 'Будет удалено:'
Write-Host "  папка программы $Dir (настройки, cookies.txt, свой Python, ffmpeg)"
Write-Host '  ярлык на рабочем столе и ссылка udl-downloader: (её открывает расширение браузера)'
$Downloads = Join-Path $Dir 'Скачанное'
$files = @(if (Test-Path $Downloads) { Get-ChildItem $Downloads -Recurse -File -Force })
if ($files.Count) { Write-Host "  в папке «Скачанное» внутри программы файлов: $($files.Count) — про них спрошу отдельно" }
try {
    $chosen = (Get-Content "$Dir\settings.json" -Raw -Encoding UTF8 | ConvertFrom-Json).download_dir
    if ($chosen -and -not $chosen.StartsWith("$Dir\", [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Файлы в выбранной вами папке загрузок $chosen остаются на месте."
    }
} catch {}
Write-Host ''
if (-not (Ask 'Удалить программу?')) { Done 'Ничего не удалено.' }

# Программа и её ffmpeg/Deno — это процессы из папки программы
$running = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith("$Dir\", [StringComparison]::OrdinalIgnoreCase) })
if ($running.Count) {
    Write-Host 'Закрываю программу (незаконченные загрузки прервутся)...'
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    $running | ForEach-Object { $_.WaitForExit(5000) | Out-Null }
}

if ($files.Count) {
    if (Ask "Удалить и скачанное ($($files.Count) файлов в папке «Скачанное»)? Н — перенесу её на рабочий стол") {
        Write-Host 'Скачанное будет удалено вместе с программой.'
    } else {
        $target = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Скачанное (Универсальный скачиватель)'
        $n = 2
        while (Test-Path $target) { $target = Join-Path ([Environment]::GetFolderPath('Desktop')) "Скачанное (Универсальный скачиватель) $n"; $n++ }
        try {
            # Move-Item не переносит папку на другой диск — тогда копируем
            try { Move-Item $Downloads $target -ErrorAction Stop }
            catch { Copy-Item $Downloads $target -Recurse -Force -ErrorAction Stop }
            Write-Host "Скачанное перенесено: $target"
        } catch {
            Done "Не удалось перенести «Скачанное»: $($_.Exception.Message)`nПрограмма не удалена — перенесите папку сами и запустите Удалить.bat снова." Red
        }
    }
}

$link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Универсальный скачиватель.lnk'
if (Test-Path $link) {
    try {
        if (Test-Ours (New-Object -ComObject WScript.Shell).CreateShortcut($link).Arguments) {
            Remove-Item $link -Force -ErrorAction Stop
            Write-Host 'Ярлык с рабочего стола удалён.'
        } else {
            Write-Host 'Ярлык на рабочем столе ведёт в другую копию программы — оставляю.'
        }
    } catch { Write-Host "Не удалось удалить ярлык: $($_.Exception.Message)" -ForegroundColor Yellow }
}

$key = 'HKCU:\Software\Classes\udl-downloader'
if (Test-Path $key) {
    try {
        if (Test-Ours (Get-ItemProperty "$key\shell\open\command" -ErrorAction SilentlyContinue).'(default)') {
            Remove-Item $key -Recurse -Force -ErrorAction Stop
            Write-Host 'Ссылка udl-downloader: удалена.'
        }
    } catch { Write-Host "Не удалось удалить ссылку udl-downloader: $($_.Exception.Message)" -ForegroundColor Yellow }
}

# Временные файлы прослушивания (песня целиком для SoundCloud/Rutube)
Get-ChildItem $env:TEMP -Directory -Filter 'udl-listen-*' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host 'Удаляю папку программы...'
# Файлы только что закрытых процессов освобождаются не сразу — пробуем несколько раз
for ($i = 0; $i -lt 10 -and (Test-Path $Dir); $i++) {
    if ($i) { Start-Sleep -Milliseconds 700 }
    Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $Dir) {
    $left = @(Get-ChildItem $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)
    Write-Host "Не всё удалилось (файлов осталось: $($left.Count)) — какой-то файл занят другой программой." -ForegroundColor Yellow
    Write-Host "Перезагрузите компьютер и удалите папку сами: $Dir"
}

Write-Host ''
Write-Host 'Если ставили расширение «Скачать в программе» — удалите его в браузере на странице расширений'
Write-Host '(chrome://extensions, edge://extensions, browser://extensions, opera://extensions), а кнопку'
Write-Host '«Скачать в программе» на панели закладок — правым щелчком → «Удалить».'
Done 'Универсальный скачиватель удалён.' Green
