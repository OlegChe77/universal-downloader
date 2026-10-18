# Собирает архив для раздачи: dist\Универсальный скачиватель.zip с папкой программы внутри.
# Python, пакеты и ffmpeg в архив не входят — их ставит start.ps1 при первом запуске.
# Личное (настройки, cookies, журнал, загрузки) и файлы разработки не попадают.
# Запуск: powershell -NoProfile -ExecutionPolicy Bypass -File build-zip.ps1
# Файл должен быть в UTF-8 с BOM, иначе Windows PowerShell 5.1 испортит русский текст.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

$Name = 'Универсальный скачиватель'
$Root = $PSScriptRoot
$Out = Join-Path $Root "dist\$Name.zip"

# Имена в корне папки программы, которые не берём
$Skip = @(
    'settings.json', 'program.log', 'cookies.txt', 'Скачанное', 'tests', 'CLAUDE.md',  # личное и разработка
    '.venv', 'python', 'python.new', 'bin', 'dist', '.claude', 'build-zip.ps1',          # ставится само / сборка
    '.git', '.gitignore', '.gitattributes', 'docs'                                                   # только для GitHub
)

$files = Get-ChildItem $Root -Force | Where-Object { $Skip -notcontains $_.Name -and $_.Name -notlike 'CLAUDE.md.tmp*' } |
    ForEach-Object { if ($_.PSIsContainer) { Get-ChildItem $_.FullName -Recurse -File -Force } else { $_ } } |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' }

foreach ($need in 'app.py', 'start.ps1', 'launch.vbs', 'Запуск.bat', 'requirements.txt', 'web\index.html') {
    if (-not (Test-Path (Join-Path $Root $need))) { throw "Нет файла $need" }
}

New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
if (Test-Path $Out) { Remove-Item $Out }
# Сами пишем записи с «/»: Compress-Archive в PowerShell 5.1 ставит «\», и другие программы видят их как часть имени.
# Русские имена .NET пишет в UTF-8 с флагом — Проводник Windows их читает.
$zip = [System.IO.Compression.ZipFile]::Open($Out, 'Create')
try {
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($Root.Length + 1) -replace '\\', '/'
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, "$Name/$rel", 'Optimal') | Out-Null
        Write-Host "  $rel"
    }
} finally {
    $zip.Dispose()
}
$size = [math]::Round((Get-Item $Out).Length / 1KB)
Write-Host "Готово: $Out ($($files.Count) файлов, $size КБ)" -ForegroundColor Green
