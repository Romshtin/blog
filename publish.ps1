# ============================================================
#  Публикация блога на VPS.
#  Запуск: powershell -ExecutionPolicy Bypass -File publish.ps1
#  Конфиг: nastroyki-servera.txt рядом со скриптом.
#
#  Что делает:
#   1. Если в папке Загрузки лежит свежий posts*.js (скачанный из
#      admin.html) новее, чем posts.js в папке блога — копирует его
#      сюда (снимает ловушку "скачал, но не перенёс").
#   2. Загружает на VPS по scp HTML-файлы:
#      index.html, post.html, obo-mne.html, otzyvy.html, zapis.html
#      (с 2026-09-08 ещё politika-pd.html — согласие на обработку ПД)
#      (с 2026-10-02 ещё statyi.html — раздел «Профессиональные статьи»,
#      на странице лента постов категории «Профессиональные статьи»)
#      плюс sitemap.xml, robots.txt (поисковые консоли, 2026-09-04),
#      favicon.svg и og-preview.png (SEO-разметка, 2026-09-04)
#      (с 2026-10-06 ещё <KEY>.txt — ключ-файл IndexNow)
#   3. posts.js: если был свежий скачанный из Загрузок — грузит его
#      (запасной путь). Иначе НЕ грузит локальный, а наоборот скачивает
#      серверный сюда: с 2026-09-03 posts.js публикуется прямо из
#      admin.html (POST /api/admin/save), и локальный файл может быть
#      старее серверного — грузить его значило бы затереть свежую
#      публикацию из админки (такое уже случилось 2026-09-03).
#   4. При dry_run=yes в конфиге — только печатает команды.
# ============================================================

$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }

# --- конфиг ---
$cfg = @{}
Get-Content -Path (Join-Path $root 'nastroyki-servera.txt') -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $i = $line.IndexOf('=')
    if ($i -gt 0) { $cfg[$line.Substring(0, $i).Trim().ToLower()] = $line.Substring($i + 1).Trim() }
}

$vHost      = $cfg['host']
$vPort      = if ($cfg['port']) { $cfg['port'] } else { '22' }
$vUser      = $cfg['user']
$vKey       = $cfg['key']
$vRemoteDir = $cfg['remote_dir']
$vDomain    = if ($cfg['domain']) { $cfg['domain'] } else { $vHost }
$dryRun     = ($cfg['dry_run'] -eq 'yes')

if (-not $vHost -or -not $vUser -or -not $vKey -or -not $vRemoteDir) {
    Write-Host 'ОШИБКА: в nastroyki-servera.txt не заполнены host / user / key / remote_dir.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $vKey)) {
    Write-Host ('ОШИБКА: файл ключа не найден: ' + $vKey) -ForegroundColor Red
    exit 1
}

# --- белый список файлов ---
$files = @('index.html', 'post.html', 'obo-mne.html', 'otzyvy.html', 'zapis.html', 'statyi.html', 'politika-pd.html', 'posts.js', 'sitemap.xml', 'robots.txt',
           'yandex_307732405a18830b.html', 'google19c986c8ba34eac8.html', 'favicon.svg', 'og-preview.png',
           '7f95ebf3b18e4943b308ded2c28ec006.txt')
$missing = @($files | Where-Object { -not (Test-Path (Join-Path $root $_)) })
if ($missing.Count -gt 0) {
    Write-Host ('ОШИБКА: в папке блога нет файлов: ' + ($missing -join ', ')) -ForegroundColor Red
    exit 1
}

# --- подхват свежего posts.js из Загрузок ---
# Реальную папку загрузок спрашиваем у самой Windows: в зависимости от
# системы она называется «Загрузки» или «Downloads», поэтому смотрим и то,
# и другое (на этой машине папка — Downloads, английская).
$dlDirs = @()
try {
    $p = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path
    if ($p) { $dlDirs += $p }
} catch {}
$dlDirs += (Join-Path $env:USERPROFILE 'Загрузки'), (Join-Path $env:USERPROFILE 'Downloads')
$dlDirs = $dlDirs | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
$blogPosts = Join-Path $root 'posts.js'
$fresh = $dlDirs | ForEach-Object {
             Get-ChildItem -Path $_ -Filter 'posts*.js' -ErrorAction SilentlyContinue
         } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
# posts.js грузим вверх только если владелец явно скачал свежий из админки
# (запасной путь); иначе локальный считается зеркалом и обновляется с сервера
$pushPosts = $false
if ($fresh -and $fresh.LastWriteTime -gt (Get-Item $blogPosts).LastWriteTime) {
    if ($dryRun) {
        Write-Host ('DRY RUN: перенёс бы свежий ' + $fresh.Name + ' из «' + $fresh.DirectoryName + '» в posts.js')
    } else {
        Copy-Item -Path $fresh.FullName -Destination $blogPosts -Force
        Write-Host ('Свежий ' + $fresh.Name + ' из «' + $fresh.DirectoryName + '» перенесён в posts.js блога.')
    }
    $pushPosts = $true
} elseif ($fresh) {
    Write-Host 'Свежего posts.js в Загрузках нет — на сервере он главный (публикуется из админки),'
    Write-Host 'поэтому локальный сейчас будет обновлён с сервера, а не наоборот.'
}

$sshArgs = @('-i', $vKey, '-P', $vPort, '-o', 'StrictHostKeyChecking=accept-new')
$target  = $vUser + '@' + $vHost + ':' + $vRemoteDir
$upFiles    = if ($pushPosts) { $files } else { $files | Where-Object { $_ -ne 'posts.js' } }
$localPaths = @($upFiles | ForEach-Object { Join-Path $root $_ })

if ($dryRun) {
    Write-Host 'DRY RUN — команды, которые были бы выполнены:'
    Write-Host ('scp ' + ($sshArgs -join ' ') + ' ' + ($localPaths -join ' ') + ' ' + $target + '/')
    if (-not $pushPosts) {
        Write-Host ('scp ' + ($sshArgs -join ' ') + ' ' + $target + '/posts.js ' + (Join-Path $root 'posts.js'))
    }
    exit 0
}

# --- scp ---
$scp = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
if (-not (Test-Path $scp)) {
    Write-Host 'ОШИБКА: scp.exe не найден в C:\Windows\System32\OpenSSH.' -ForegroundColor Red
    Write-Host 'Установите клиент OpenSSH: Параметры — Приложения — Дополнительные компоненты — Добавить компонент — "Клиент OpenSSH".' -ForegroundColor Red
    exit 1
}

Write-Host ('Загружаю ' + $upFiles.Count + ' файлов на ' + $vHost + ':' + $vRemoteDir + ' ...')
& $scp @sshArgs @localPaths ($target + '/')
$code = $LASTEXITCODE

if ($code -ne 0) {
    if ($code -eq 255) {
        Write-Host 'ОШИБКА: подключение не удалось (код 255).' -ForegroundColor Red
        Write-Host 'Если выше было "Permission denied" — проблема с ключом;' -ForegroundColor Red
        Write-Host 'иначе — сервер недоступен, проверьте интернет.' -ForegroundColor Red
    } else {
        Write-Host ('ОШИБКА: загрузка не удалась (код ' + $code + ') — смотрите сообщения scp выше.') -ForegroundColor Red
    }
    exit 1
}

# Без свежего скачанного posts.js — тянем серверный сюда (зеркало), не наоборот
if (-not $pushPosts) {
    Write-Host 'Обновляю локальный posts.js с сервера...'
    & $scp @sshArgs ($target + '/posts.js') (Join-Path $root 'posts.js')
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'ВНИМАНИЕ: не удалось скачать серверный posts.js — локальный остался прежним.' -ForegroundColor Yellow
    } else {
        Write-Host 'Локальный posts.js обновлён с сервера.'
    }
}

Write-Host ''
Write-Host ('Готово: блог опубликован на https://' + $vDomain) -ForegroundColor Green