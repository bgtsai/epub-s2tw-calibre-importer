# ============================================================
# Version: 1.0.6
# ============================================================
# 安裝腳本：檢查/安裝 Python 與 opencc-python-reimplemented，並在腳本所在目錄產生捷徑
#
# 冪等性：所有檢查步驟都是「先確認有沒有，沒有才動手」，
# 重複執行（例如重灌電腦後重跑）結果應該一致，不會因為已經裝過而出錯。
#
# 全系統安裝需要系統管理員權限，未以管理員身分執行時會自動重新以管理員身分啟動一次。

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:ScriptVersion = '1.0.6'   # 對照 run.ps1 標題列的版本，確認抓到的是同一批檔案
$PythonInstallerVersion = '3.12.7'
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonInstallerVersion/python-$PythonInstallerVersion-amd64.exe"

# 有些較舊的 PowerShell/.NET 環境，Invoke-WebRequest 預設的 TLS 版本協商不含 TLS 1.2，
# 連 python.org 下載安裝程式時會直接丟 SSL/TLS 連線錯誤——這是很常見的坑，先明確指定排除掉。
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Test-IsAdmin {
    $currentUser = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $currentUser.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PythonCmd {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python') }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', '-3') }
    return $null
}

function Invoke-Python {
    # 見 run.ps1 同名函式的註解：陣列只有 1 個元素時 1..0 範圍切片會出錯，
    # 額外參數要另外判斷，不能直接切片。
    param([string[]]$PythonCmd, [string[]]$ExtraArgs)
    $exe = $PythonCmd[0]
    $extra = if ($PythonCmd.Count -gt 1) { $PythonCmd[1..($PythonCmd.Count - 1)] } else { @() }
    & $exe @extra @ExtraArgs
}

# 全部步驟包在 try/catch 裡：管理員權限視窗跑完（不管成功或出錯）預設會直接關掉，
# 不包起來的話，出錯訊息會跟著視窗一閃而過、完全看不到內容。
$exitCode = 0
try {
    Write-Output "=== EPUB 簡轉繁匯入 Calibre 安裝程式 v$($script:ScriptVersion) ==="

    # --- 步驟 1：確認以系統管理員身分執行（全系統安裝 Python 需要） ---
    if (-not (Test-IsAdmin)) {
        Write-Output '需要系統管理員權限才能安裝 Python，重新以管理員身分啟動...'
        $psi = @{
            FilePath     = 'powershell.exe'
            ArgumentList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$($MyInvocation.MyCommand.Path)`"")
            Verb         = 'RunAs'
        }
        # 使用者若在 UAC 視窗按「否」，Start-Process 會直接丟例外，被下面的 catch 接住並顯示，
        # 不會讓視窗無聲無息地消失。
        Start-Process @psi
        Write-Output '已在新視窗以系統管理員身分啟動安裝流程，這個視窗可以關閉了。'
        exit 0
    }

    # --- 步驟 2：檢查 Python 是否已安裝，沒有才下載安裝 ---
    $pythonCmd = Get-PythonCmd
    if (-not $pythonCmd) {
        Write-Output "找不到 Python，開始下載安裝程式（版本 $PythonInstallerVersion）..."
        $installerPath = Join-Path $env:TEMP "python-$PythonInstallerVersion-amd64.exe"
        Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $installerPath -UseBasicParsing

        Write-Output '靜默安裝中（全系統安裝，會加入 PATH）...'
        $proc = Start-Process -FilePath $installerPath -ArgumentList @(
            '/quiet', 'InstallAllUsers=1', 'PrependPath=1'
        ) -Wait -PassThru
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue

        if ($proc.ExitCode -ne 0) {
            throw "Python 安裝失敗，exit code: $($proc.ExitCode)"
        }

        # 安裝完 PATH 要重新整理，同一個 process 讀不到剛更新的系統 PATH，
        # 從登錄檔重新組一次目前 session 的 PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')

        $pythonCmd = Get-PythonCmd
        if (-not $pythonCmd) {
            throw 'Python 安裝完成但仍偵測不到指令，可能需要重新開啟終端機或重新登入才會生效。'
        }
        Write-Output 'Python 安裝完成。'
    }
    else {
        Write-Output "偵測到已安裝的 Python（指令：$($pythonCmd -join ' ')），跳過安裝。"
    }

    # --- 步驟 3：檢查 opencc 套件，沒有才 pip install ---
    Invoke-Python -PythonCmd $pythonCmd -ExtraArgs @('-c', 'import opencc') 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output '偵測到已安裝 opencc-python-reimplemented，跳過安裝。'
    }
    else {
        Write-Output '安裝 opencc-python-reimplemented...'
        Invoke-Python -PythonCmd $pythonCmd -ExtraArgs @('-m', 'pip', 'install', 'opencc-python-reimplemented')
        if ($LASTEXITCODE -ne 0) {
            throw 'opencc-python-reimplemented 安裝失敗，請檢查上方 pip 輸出訊息。'
        }
        Write-Output 'opencc-python-reimplemented 安裝完成。'
    }

    # --- 步驟 4：在腳本所在目錄產生捷徑，指向 run.ps1 ---
    $shortcutPath = Join-Path $ScriptDir '執行_EPUB簡轉繁匯入Calibre.lnk'
    $targetScript = Join-Path $ScriptDir 'run.ps1'

    $wsh = New-Object -ComObject WScript.Shell
    $shortcut = $wsh.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = 'powershell.exe'
    # -WindowStyle Hidden 讓 console 不要一啟動就閃出來；run.ps1 內部另外用
    # Win32 API 把 console 視窗徹底隱藏，介面統一在 WinForms 主視窗
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$targetScript`""
    $shortcut.WorkingDirectory = $ScriptDir
    $shortcut.WindowStyle = 7   # 最小化啟動，進一步減少閃窗
    $shortcut.Description = 'EPUB 簡轉繁並匯入 Calibre'
    $shortcut.Save()

    Write-Output "捷徑已建立：$shortcutPath"

    # --- 步驟 5：建立使用者自己的設定檔 config.ini ---
    # 發布包只附範本 config.example.ini，更新時整包覆蓋不會蓋掉使用者的 config.ini。
    # 已經有 config.ini（重複安裝或更新）就不動它。
    $configPath = Join-Path $ScriptDir 'config.ini'
    $configExamplePath = Join-Path $ScriptDir 'config.example.ini'
    if (Test-Path -LiteralPath $configPath) {
        Write-Output "已有設定檔，保留不動：$configPath"
    }
    elseif (Test-Path -LiteralPath $configExamplePath) {
        Copy-Item -LiteralPath $configExamplePath -Destination $configPath
        Write-Output "已建立設定檔：$configPath"
        Write-Output '已用記事本開啟，請把裡面的「<你的路徑>」改成你自己的資料夾後存檔。'
        Start-Process -FilePath 'notepad.exe' -ArgumentList "`"$configPath`""
    }
    else {
        Write-Output '找不到 config.example.ini，請確認下載的檔案是否完整。'
    }

    Write-Output '安裝完成，可以把這個捷徑搬到桌面或任何想放的位置。'
}
catch {
    Write-Output ''
    Write-Output '===== 安裝過程發生錯誤 ====='
    Write-Output $_.Exception.Message
    Write-Output ''
    Write-Output $_.ScriptStackTrace
    $exitCode = 1
}

Write-Output ''
Read-Host '按 Enter 鍵結束'
exit $exitCode
