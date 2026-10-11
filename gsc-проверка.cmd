@echo off
rem Prepares Google Search Console URL Inspection for the newest blog post.
rem The Google deep link cannot point at a URL directly (its "id" parameter
rem is an opaque token from the URL Inspection API), so this shortcut instead:
rem   1) copies the post URL to the clipboard,
rem   2) opens the Search Console for the property
rem and the owner pastes it into "URL Inspection" (Ctrl+V, Enter), then
rem clicks "Request indexing" (~10-12 URLs/day limit).
rem Usage (ASCII only):
rem   with post id argument:      gsc-*.cmd 12
rem   without argument: newest post id taken from the live sitemap.xml
rem   (fallback: maximal id in the local posts.js)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$b='https://roman-kirillov.ru'; $id='%~1'; if(-not $id){ try { $r=Invoke-WebRequest -UseBasicParsing ($b+'/sitemap.xml'); [xml]$s=$r.Content; $loc=$s.urlset.url | ForEach-Object { [string]$_.loc } | Where-Object { $_ -match 'id=(\d+)' } | Select-Object -First 1; if($loc){ $id=[regex]::Match($loc,'id=(\d+)').Groups[1].Value } } catch {}; if(-not $id){ try { $t=[IO.File]::ReadAllText((Join-Path (Get-Location) 'posts.js')); $ids=[regex]::Matches($t,'id\x22:\s*(\d+)') | ForEach-Object { [int]$_.Groups[1].Value }; $m=$ids | Measure-Object -Maximum; if($m.Maximum){ $id=[string]$m.Maximum } } catch {} }; }; if(-not $id){ Write-Host 'Cannot determine the newest post id. Run with an argument: %~nx0 12'; exit 1 }; $post=$b+'/post.html?id=' + $id; Set-Clipboard -Value $post; Write-Host 'Copied to clipboard:' $post; Start-Process ('https://search.google.com/search-console?resource_id=' + [uri]::EscapeDataString($b + '/')); Write-Host 'Paste into URL Inspection (Ctrl+V, Enter), then Request indexing.'"