"""Значок aiflow в области уведомлений Windows и ярлык в меню «Пуск».

Значок рисует и держит Windows PowerShell (он есть в любой Windows): служба запускает его скрипт рядом с собой,
скрипт раз в несколько секунд спрашивает панель, сколько задач ждёт человека, и выходит вместе со службой.
Сторонних пакетов нет.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

TRAY_PS = r'''param([string]$Url, [int]$ServicePid, [string]$HomeDir, [string]$Browser, [string]$Python, [string]$Script)
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# один значок на пользователя: новый ждёт, пока значок прежней службы уйдёт вместе с ней
$mutex = New-Object System.Threading.Mutex($false, 'Local\aiflow-tray')
try { if (-not $mutex.WaitOne(20000)) { exit } } catch [System.Threading.AbandonedMutexException] { }

function New-AiflowIcon([int]$count) {
  $bmp = New-Object System.Drawing.Bitmap 32, 32
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  if ($count -gt 0) { $color = [System.Drawing.Color]::FromArgb(217, 89, 38) } else { $color = [System.Drawing.Color]::FromArgb(42, 120, 214) }
  $brush = New-Object System.Drawing.SolidBrush $color
  $g.FillEllipse($brush, 1, 1, 30, 30)
  if ($count -gt 9) { $text = '9+' } elseif ($count -gt 0) { $text = "$count" } else { $text = 'a' }
  if ($text.Length -gt 1) { $size = 15 } else { $size = 20 }
  $font = New-Object System.Drawing.Font('Segoe UI', $size, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
  $fmt = New-Object System.Drawing.StringFormat
  $fmt.Alignment = [System.Drawing.StringAlignment]::Center
  $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
  $g.DrawString($text, $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF 0, 0, 32, 31), $fmt)
  $g.Dispose(); $font.Dispose(); $brush.Dispose()
  return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}

function Get-Tray {
  try {
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [System.Text.Encoding]::UTF8
    return ($wc.DownloadString("$Url/api/tray") | ConvertFrom-Json)
  } catch { return $null }
}

function Send-Action($body) {
  try {
    $token = (Get-Content -Raw -Encoding UTF8 (Join-Path $HomeDir 'panel.token')).Trim()
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [System.Text.Encoding]::UTF8
    $wc.Headers.Add('X-Aiflow-Token', $token)
    $wc.Headers.Add('Content-Type', 'application/json')
    [void]$wc.UploadString("$Url/api/action", ($body | ConvertTo-Json -Compress))
  } catch {
    $script:ni.ShowBalloonTip(5000, 'aiflow', 'Не получилось: ' + $_.Exception.Message, [System.Windows.Forms.ToolTipIcon]::Warning)
  }
}

function Open-Panel {
  if ($Browser -and (Test-Path $Browser)) { Start-Process -FilePath $Browser -ArgumentList "--app=$Url/", '--window-size=1320,900' }
  else { Start-Process "$Url/" }
}

function Stop-Tray {
  $script:timer.Stop()
  $script:ni.Visible = $false
  $script:ni.Dispose()
  [System.Windows.Forms.Application]::Exit()
}

function Short([string]$s, [int]$n) { if ($s.Length -gt $n) { return $s.Substring(0, $n - 1) + '…' } return $s }

function Build-Menu {
  $menu = $script:menu
  $menu.Items.Clear()
  $open = $menu.Items.Add('Открыть панель')
  $open.Font = New-Object System.Drawing.Font($open.Font, [System.Drawing.FontStyle]::Bold)
  $open.add_Click({ Open-Panel })
  $state = Get-Tray
  if ($state -and $state.waiting -and @($state.waiting).Count) {
    [void]$menu.Items.Add('-')
    $head = $menu.Items.Add('Ждут вас — открыть чат задачи:')
    $head.Enabled = $false
    foreach ($w in @($state.waiting)) {
      $item = $menu.Items.Add((Short ("$($w.project) · $($w.id) — $($w.needs)") 90))
      $item.Tag = @{ action = 'open_session'; project = $w.key; id = $w.id }
      $item.add_Click({ Send-Action $this.Tag })
    }
  }
  if ($state -and $state.projects -and @($state.projects).Count) {
    [void]$menu.Items.Add('-')
    foreach ($p in @($state.projects)) {
      $item = $menu.Items.Add("Дать задачу: $($p.name)")
      $item.Tag = @{ action = 'open_orchestrator'; project = $p.key }
      $item.add_Click({ Send-Action $this.Tag })
    }
  }
  [void]$menu.Items.Add('-')
  $stop = $menu.Items.Add('Остановить aiflow')
  $stop.add_Click({
    Start-Process -FilePath $Python -ArgumentList '-X', 'utf8', "`"$Script`"", 'down' -WindowStyle Hidden
    Stop-Tray
  })
}

$script:count = -1
function Update-Tray {
  if (-not (Get-Process -Id $ServicePid)) { Stop-Tray; return }
  $state = Get-Tray
  if (-not $state) { return }
  $n = @($state.waiting).Count
  if ($n -ne $script:count) {
    $script:count = $n
    $script:ni.Icon = New-AiflowIcon $n
  }
  if ($n -gt 0) { $tip = "aiflow: ждут вас — $n" }
  elseif ($state.active -gt 0) { $tip = "aiflow: в работе — $($state.active)" }
  else { $tip = 'aiflow: очередь пуста' }
  if ($state.paused) { $tip += ' (пауза)' }
  $script:ni.Text = $tip
}

$script:ni = New-Object System.Windows.Forms.NotifyIcon
$script:ni.Icon = New-AiflowIcon 0
$script:ni.Text = 'aiflow'
$script:menu = New-Object System.Windows.Forms.ContextMenuStrip
$script:menu.add_Opening({ Build-Menu })
[void]$script:menu.Items.Add('Открыть панель')
$script:ni.ContextMenuStrip = $script:menu
$script:ni.add_MouseClick({ if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Open-Panel } })
$script:ni.Visible = $true
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 5000
$script:timer.add_Tick({ Update-Tray })
$script:timer.Start()
Update-Tray
[System.Windows.Forms.Application]::Run()
$mutex.ReleaseMutex()
'''

# Значок приложения для ярлыка: тот же круг, 256 px, PNG внутри контейнера ICO.
ICON_PS = r'''$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap 256, 256
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(42, 120, 214))
$g.FillEllipse($brush, 8, 8, 240, 240)
$font = New-Object System.Drawing.Font('Segoe UI', 160, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$fmt = New-Object System.Drawing.StringFormat
$fmt.Alignment = [System.Drawing.StringAlignment]::Center
$fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
$g.DrawString('a', $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF 0, 0, 256, 244), $fmt)
$g.Dispose()
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
$png = $ms.ToArray()
$out = New-Object System.IO.MemoryStream
$w = New-Object System.IO.BinaryWriter $out
$w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]1)
$w.Write([byte]0); $w.Write([byte]0); $w.Write([byte]0); $w.Write([byte]0)
$w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$png.Length); $w.Write([uint32]22)
$w.Write($png); $w.Flush()
[System.IO.File]::WriteAllBytes($env:AIFLOW_ICO, $out.ToArray())
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($env:AIFLOW_LNK)
$lnk.TargetPath = $env:AIFLOW_LNK_TARGET
$lnk.Arguments = $env:AIFLOW_LNK_ARGS
$lnk.WorkingDirectory = $env:AIFLOW_LNK_DIR
$lnk.IconLocation = $env:AIFLOW_ICO
$lnk.Description = 'aiflow: очередь задач Claude Code'
$lnk.Save()
'''


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


def tray_command(script: Path, url: str, pid: int, home: Path, browser: str, python: str, aiflow_script: Path) -> list[str]:
    return [_powershell() or "powershell.exe", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Url", url.rstrip("/"), "-ServicePid", str(pid), "-HomeDir", str(home),
            "-Browser", browser, "-Python", python, "-Script", str(aiflow_script)]


def start_tray(url: str, pid: int, home: Path, browser: str, python: str, aiflow_script: Path) -> bool:
    """Запустить значок рядом со службой. Только Windows; False — показать нечем."""
    if os.name != "nt" or not url or not _powershell():
        return False
    script = home / "tray.ps1"
    try:
        # BOM: Windows PowerShell 5.1 читает файл без него в кодировке ANSI и портит кириллицу
        script.write_text(TRAY_PS, encoding="utf-8-sig")
        subprocess.Popen(tray_command(script, url, pid, home, browser, python, aiflow_script), cwd=str(home),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def shortcut_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "aiflow.lnk"


def shortcut(enable: bool, home: Path, python: str, launcher: Path) -> str:
    """Ярлык aiflow в меню «Пуск»: поднимает службу и открывает окно панели. Только Windows."""
    lnk, ico = shortcut_path(), home / "aiflow.ico"
    if not enable:
        lnk.unlink(missing_ok=True)
        ico.unlink(missing_ok=True)
        return str(lnk)
    ps = _powershell()
    if os.name != "nt" or not ps:
        raise OSError("ярлык в меню «Пуск» делается только в Windows")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AIFLOW_ICO=str(ico), AIFLOW_LNK=str(lnk), AIFLOW_LNK_TARGET=python,
               AIFLOW_LNK_ARGS=f'"{launcher}" open', AIFLOW_LNK_DIR=str(home))
    script = home / "shortcut.ps1"
    script.write_text(ICON_PS, encoding="utf-8-sig")
    try:
        p = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           env=env, timeout=60, stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        script.unlink(missing_ok=True)
    if p.returncode != 0 or not lnk.is_file():
        raise OSError("не удалось создать ярлык: " + (p.stderr or b"").decode("utf-8", "replace").strip()[-300:])
    return str(lnk)
