# Удаление Универсального скачивателя: закрывает программу, убирает ярлык, ссылку udl-downloader: и папку программы.
# Вызывается из Удалить.bat. Файл должен быть в UTF-8 с BOM, иначе Windows PowerShell 5.1 испортит русский текст.

# Язык сообщений — как у программы: «lang» в settings.json, пусто — язык Windows (как в i18n.py)
$Lang = ''
try { $Lang = (Get-Content (Join-Path $PSScriptRoot 'settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json).lang } catch {}
if ($Lang -ne 'ru' -and $Lang -ne 'en') {
    $Lang = if ((Get-UICulture).TwoLetterISOLanguageName -in 'ru', 'uk', 'be', 'kk', 'ky', 'uz', 'tg') { 'ru' } else { 'en' }
}
function T($ru, $en) { if ($Lang -eq 'en') { $en } else { $ru } }
$AppName = T 'Универсальный скачиватель' 'Universal Downloader'
$Host.UI.RawUI.WindowTitle = (T 'Удаление: ' 'Uninstall: ') + $AppName
$Dir = $PSScriptRoot
Set-Location $env:TEMP  # из своей папки — иначе она занята и не удалится

function Done($message, $color = 'Gray') {
    Write-Host ''
    if ($message) { Write-Host $message -ForegroundColor $color }
    Read-Host (T 'Нажмите Enter, чтобы закрыть окно' 'Press Enter to close the window')
    exit
}

function Ask($question) {
    # «Д» в английской раскладке — «L»
    return (Read-Host "$question $(T '(Д/Н)' '(Y/N)')").Trim() -match '^(д|да|l|y|yes)$'
}

function Test-Ours($text) {
    # Ярлык и ссылка могли остаться от другой копии программы — их не трогаем
    return $text -and $text.IndexOf("$Dir\launch.vbs", [StringComparison]::OrdinalIgnoreCase) -ge 0
}

if (-not (Test-Path "$Dir\app.py") -or -not (Test-Path "$Dir\launch.vbs")) { Done (T 'Это не папка программы — ничего не удаляю.' 'This is not the program folder — deleting nothing.') Red }
if (Test-Path "$Dir\.git") { Done (T 'Это папка разработки (в ней .git) — не удаляю.' 'This is a development folder (it has .git) — not deleting.') Red }

Write-Host (T 'Будет удалено:' 'Will be removed:')
Write-Host (T "  папка программы $Dir (настройки, cookies.txt, свой Python, ffmpeg)" "  the program folder $Dir (settings, cookies.txt, its own Python, ffmpeg)")
Write-Host (T '  ярлык на рабочем столе и ссылка udl-downloader: (её открывает расширение браузера)' '  the desktop shortcut and the udl-downloader: link (the browser extension opens it)')
$Downloads = Join-Path $Dir 'Скачанное'
$files = @(if (Test-Path $Downloads) { Get-ChildItem $Downloads -Recurse -File -Force })
if ($files.Count) { Write-Host (T "  в папке «Скачанное» внутри программы файлов: $($files.Count) — про них спрошу отдельно" "  files in the «Скачанное» (Downloads) folder inside the program: $($files.Count) — I will ask about them separately") }
try {
    $chosen = (Get-Content "$Dir\settings.json" -Raw -Encoding UTF8 | ConvertFrom-Json).download_dir
    if ($chosen -and -not $chosen.StartsWith("$Dir\", [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host (T "Файлы в выбранной вами папке загрузок $chosen остаются на месте." "Files in your chosen download folder $chosen stay in place.")
    }
} catch {}
Write-Host ''
if (-not (Ask (T 'Удалить программу?' 'Uninstall the program?'))) { Done (T 'Ничего не удалено.' 'Nothing was removed.') }

# Программа и её ffmpeg/Deno — это процессы из папки программы
$running = @(Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith("$Dir\", [StringComparison]::OrdinalIgnoreCase) })
if ($running.Count) {
    Write-Host (T 'Закрываю программу (незаконченные загрузки прервутся)...' 'Closing the program (unfinished downloads will stop)...')
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    $running | ForEach-Object { $_.WaitForExit(5000) | Out-Null }
}

if ($files.Count) {
    if (Ask (T "Удалить и скачанное ($($files.Count) файлов в папке «Скачанное»)? Н — перенесу её на рабочий стол" "Delete the downloads too ($($files.Count) files in the «Скачанное» folder)? N — I will move it to the desktop")) {
        Write-Host (T 'Скачанное будет удалено вместе с программой.' 'The downloads will be removed with the program.')
    } else {
        $target = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Скачанное (Универсальный скачиватель)'
        $n = 2
        while (Test-Path $target) { $target = Join-Path ([Environment]::GetFolderPath('Desktop')) "Скачанное (Универсальный скачиватель) $n"; $n++ }
        try {
            # Move-Item не переносит папку на другой диск — тогда копируем
            try { Move-Item $Downloads $target -ErrorAction Stop }
            catch { Copy-Item $Downloads $target -Recurse -Force -ErrorAction Stop }
            Write-Host ((T 'Скачанное перенесено: ' 'Downloads moved: ') + $target)
        } catch {
            Done (T "Не удалось перенести «Скачанное»: $($_.Exception.Message)`nПрограмма не удалена — перенесите папку сами и запустите Удалить.bat снова." "Could not move «Скачанное»: $($_.Exception.Message)`nThe program was not removed — move the folder yourself and run Удалить.bat again.") Red
        }
    }
}

foreach ($name in 'Универсальный скачиватель.lnk', 'Universal Downloader.lnk') {  # имя ярлыка — на языке программы
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) $name
if (Test-Path $link) {
    try {
        if (Test-Ours (New-Object -ComObject WScript.Shell).CreateShortcut($link).Arguments) {
            Remove-Item $link -Force -ErrorAction Stop
            Write-Host (T 'Ярлык с рабочего стола удалён.' 'The desktop shortcut was removed.')
        } else {
            Write-Host (T 'Ярлык на рабочем столе ведёт в другую копию программы — оставляю.' 'The desktop shortcut leads to another copy of the program — keeping it.')
        }
    } catch { Write-Host ((T 'Не удалось удалить ярлык: ' 'Could not remove the shortcut: ') + $_.Exception.Message) -ForegroundColor Yellow }
}
}

$key = 'HKCU:\Software\Classes\udl-downloader'
if (Test-Path $key) {
    try {
        if (Test-Ours (Get-ItemProperty "$key\shell\open\command" -ErrorAction SilentlyContinue).'(default)') {
            Remove-Item $key -Recurse -Force -ErrorAction Stop
            Write-Host (T 'Ссылка udl-downloader: удалена.' 'The udl-downloader: link was removed.')
        }
    } catch { Write-Host ((T 'Не удалось удалить ссылку udl-downloader: ' 'Could not remove the udl-downloader: link: ') + $_.Exception.Message) -ForegroundColor Yellow }
}

# Временные файлы прослушивания (песня целиком для SoundCloud/Rutube)
Get-ChildItem $env:TEMP -Directory -Filter 'udl-listen-*' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host (T 'Удаляю папку программы...' 'Deleting the program folder...')
# Файлы только что закрытых процессов освобождаются не сразу — пробуем несколько раз
for ($i = 0; $i -lt 10 -and (Test-Path $Dir); $i++) {
    if ($i) { Start-Sleep -Milliseconds 700 }
    Remove-Item $Dir -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $Dir) {
    $left = @(Get-ChildItem $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)
    Write-Host (T "Не всё удалилось (файлов осталось: $($left.Count)) — какой-то файл занят другой программой." "Not everything was deleted (files left: $($left.Count)) — some file is in use by another program.") -ForegroundColor Yellow
    Write-Host (T "Перезагрузите компьютер и удалите папку сами: $Dir" "Restart the computer and delete the folder yourself: $Dir")
}

Write-Host ''
Write-Host (T 'Если ставили расширение «Скачать в программе» — удалите его в браузере на странице расширений' 'If you installed the browser extension, remove it on the extensions page')
Write-Host (T '(chrome://extensions, edge://extensions, browser://extensions, opera://extensions), а кнопку' '(chrome://extensions, edge://extensions, browser://extensions, opera://extensions), and the')
Write-Host (T '«Скачать в программе» на панели закладок — правым щелчком → «Удалить».' '“Send to Downloader” bookmark — right-click → “Delete”.')
Done (T 'Универсальный скачиватель удалён.' 'Universal Downloader was uninstalled.') Green
