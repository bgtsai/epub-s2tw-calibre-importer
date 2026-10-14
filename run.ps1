# ============================================================
# Version: 1.0.2
# ============================================================
# EPUB 簡轉繁 + 匯入 Calibre 主流程
#
# 介面設計：全程只有一個 WinForms 視窗，console 視窗一開始就隱藏。
#   同一個視窗分三個階段：選項按鈕 -> 即時進度記錄 -> 總結。
#   （先前 console 與 WinForms 兩套介面並存，會互相搶焦點、一直閃，體驗很差）
#
# 流程：讀取 config -> 選項 -> 防呆檢查 -> 搬檔 -> (轉換 -> 匯入 Calibre) -> 總結
#
# 設計原則（對應對話討論定案的內容）：
#   - 所有防呆檢查都在搬動任何檔案之前跑完，寧可提早中止也不要做到一半才發現環境不對
#   - 來源資料夾找不到 -> 直接中止，不自動跳過（避免掩蓋設定錯誤）
#   - 工作用資料夾（A／錯誤／B）找不到 -> 彈窗問要不要照 config 建立，選取消就中止
#   - 撞名一律用時間戳後綴避開，絕不覆蓋、絕不丟檔
#   - 檔案去向代表責任歸屬：
#       轉換成功        -> 來源進資源回收筒、產出進 B
#       來源本身損壞    -> 留在暫存資料夾 A 原地不動（方便使用者自己打開檢查）
#       非壞檔但跳過    -> 同上，留在 A
#       轉換造成的損壞  -> 移到錯誤資料夾（代表這支程式有 bug 要修）
#   - 轉換與完整性驗證的實際邏輯都在 convert_epub.py，這支腳本只負責流程調度

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic

# 現代化視覺樣式（按鈕、捲軸等控制項的外觀）
[System.Windows.Forms.Application]::EnableVisualStyles()

# calibredb.exe 底層是 Python，輸出用 UTF-8；PowerShell 5.1 預設用系統
# 地區的 codepage（例如繁中 Windows 常是 Big5）去解讀外部程式的輸出位元組，
# 兩邊編碼對不上，遇到解不出來的字元就會變成問號。這裡讓兩邊都對齊 UTF-8。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

# 隱藏 console 視窗，讓介面統一在 WinForms 主視窗
Add-Type -Namespace Win32Util -Name WinApi -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")]
public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
$consoleHandle = [Win32Util.WinApi]::GetConsoleWindow()
if ($consoleHandle -ne [IntPtr]::Zero) {
    [void][Win32Util.WinApi]::ShowWindow($consoleHandle, 0)   # 0 = SW_HIDE
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir 'config.ini'
$ConvertScript = Join-Path $ScriptDir 'convert_epub.py'

# 版本號顯示在視窗標題，一開視窗就看得到，不用點任何按鈕或捲動記錄——
# 這樣才能一眼判斷手上這份是不是真的抓到最新版，而不是下載到舊檔案或
# 半途失敗的殘留（先前已發生過抓到 GitHub 404 錯誤頁的案例，見 27_技術雜記）
$script:ScriptVersion = '1.0.2'

# ---------- 設定檔與環境 ----------

function Read-IniConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "找不到設定檔: $Path"
    }
    $ini = @{}
    $section = ''
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith(';') -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -match '^\[(.+)\]$') {
            $section = $matches[1]
            if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }
            continue
        }
        if ($trimmed -match '^([^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            if ($section -eq '') { continue }
            $ini[$section][$key] = $value
        }
    }
    return $ini
}

function Get-SourceFolders {
    param($PathsSection)
    $folders = @()
    $i = 1
    while ($PathsSection.ContainsKey("SourceFolder$i")) {
        $folders += $PathsSection["SourceFolder$i"]
        $i++
    }
    return $folders
}

function Get-PythonCmd {
    # 相容 python / py launcher 兩種呼叫方式，不用事先確認系統上裝的是哪一種。
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python') }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', '-3') }
    return $null
}

function Invoke-Python {
    # 參數不能取名 $Args：那是 PowerShell 內建自動變數，會蓋掉傳入的值，
    # 導致實際呼叫時參數整組消失（python 因此變成無參數啟動、進入互動模式）。
    param([string[]]$PythonCmd, [string[]]$ExtraArgs)
    $exe = $PythonCmd[0]
    # 1..($Count-1) 在只有 1 個元素時會變成 1..0，反而倒著把索引 0 抓回來，
    # 所以額外參數要另外判斷，不能直接用範圍切片。
    $extra = if ($PythonCmd.Count -gt 1) { $PythonCmd[1..($PythonCmd.Count - 1)] } else { @() }
    & $exe @extra @ExtraArgs
}

function Move-ToRecycleBin {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $Path,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        )
    }
}

function Get-SanitizedFileName {
    # 過濾 Windows 不允許出現在檔名裡的字元：\ / : * ? " < > |，
    # 以及控制字元（0-31）。轉換後的書名理論上不太會冒出這些字元，
    # 但簡繁轉換或使用者自訂設定檔終究是外部輸入，不能假設一定乾淨。
    #
    # 這裡刻意寫死 Windows 的清單，不呼叫 [IO.Path]::GetInvalidFileNameChars()——
    # 那個 API 回傳的內容會依執行平台而異（Linux 上只有極少數字元被視為非法），
    # 這支腳本只會在 Windows 上跑，寫死清單才能確保行為固定、也才驗證得出來。
    param([string]$Name)
    $pattern = '[\\/:\*\?"<>\|\x00-\x1F]'
    $clean = [regex]::Replace($Name, $pattern, '_')
    # Windows 不允許檔名以空格或句點結尾
    $clean = $clean.TrimEnd(' ', '.')
    if ([string]::IsNullOrWhiteSpace($clean)) { return '未命名' }
    return $clean
}

function Get-UniqueDestinationPath {
    # 撞名時自動加時間戳後綴，絕不覆蓋、絕不丟檔
    param([string]$DestFolder, [string]$FileName)
    $dest = Join-Path $DestFolder $FileName
    if (-not (Test-Path -LiteralPath $dest)) { return $dest }

    $base = [IO.Path]::GetFileNameWithoutExtension($FileName)
    $ext = [IO.Path]::GetExtension($FileName)
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $candidate = Join-Path $DestFolder "$base`_$stamp$ext"
    $n = 1
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $DestFolder "$base`_$stamp`_$n$ext"
        $n++
    }
    return $candidate
}

# ---------- GUI 記錄輸出 ----------

function Write-Log {
    param([string]$Message, [string]$Color = 'Black')
    if ($null -eq $script:logBox) { return }
    $script:logBox.SelectionStart = $script:logBox.TextLength
    $script:logBox.SelectionLength = 0
    $script:logBox.SelectionColor = [System.Drawing.Color]::FromName($Color)
    $script:logBox.AppendText($Message + [Environment]::NewLine)
    $script:logBox.SelectionColor = $script:logBox.ForeColor
    $script:logBox.ScrollToCaret()
    # 工作跟 UI 在同一個執行緒，不主動讓出控制權的話畫面不會更新，
    # 看起來就會像整個程式當掉（先前黑視窗「沒反應」的觀感問題）
    [System.Windows.Forms.Application]::DoEvents()
}

function Show-ErrorBox {
    param([string]$Message, [string]$Title = '錯誤')
    [System.Windows.Forms.MessageBox]::Show(
        $script:form, $Message, $Title,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}

function Confirm-CreateFolder {
    param([string]$Path, [string]$Label)
    if (Test-Path -LiteralPath $Path) { return $true }
    # 指定主視窗為父視窗，確保彈在最前面、不會躲到別的視窗後面找不到
    $result = [System.Windows.Forms.MessageBox]::Show(
        $script:form,
        "$Label 資料夾不存在：`n$Path`n`n要依照 config.ini 的設定建立這個資料夾嗎？`n（選「否」會中止整個流程）",
        '資料夾不存在',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($result -eq [System.Windows.Forms.DialogResult]::Yes) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        Write-Log "已建立資料夾: $Path"
        return $true
    }
    return $false
}

function Move-EpubsToStaging {
    param([string[]]$SourceFolders, [string]$StagingFolder)
    $movedCount = 0
    foreach ($src in $SourceFolders) {
        $files = Get-ChildItem -LiteralPath $src -Filter '*.epub' -File -ErrorAction SilentlyContinue
        foreach ($f in $files) {
            $dest = Get-UniqueDestinationPath -DestFolder $StagingFolder -FileName $f.Name
            Move-Item -LiteralPath $f.FullName -Destination $dest
            $movedCount++
        }
    }
    return $movedCount
}

# ---------- 主要工作流程 ----------

function Invoke-Workflow {
    param([string]$Mode)

    Write-Log "=== 開始執行（run.ps1 v$($script:ScriptVersion)）===" 'Navy'
    $earlyPythonCmd = Get-PythonCmd
    if ($earlyPythonCmd) {
        $pyVerRaw = Invoke-Python -PythonCmd $earlyPythonCmd -ExtraArgs @($ConvertScript, 'version') 2>&1
        try {
            $pyVer = ($pyVerRaw | Select-Object -Last 1 | ConvertFrom-Json).version
            if ($pyVer) { Write-Log "convert_epub.py 版本: v$pyVer" }
        }
        catch { }
    }
    Write-Log "來源資料夾: $($script:sourceFolders -join ' | ')"
    Write-Log "暫存 (A): $($script:stagingFolder)"

    Write-Log '檢查來源資料夾是否存在...'
    $missingSources = $script:sourceFolders | Where-Object { -not (Test-Path -LiteralPath $_) }
    if ($missingSources.Count -gt 0) {
        Write-Log "以下來源資料夾查不到: $($missingSources -join ' | ')" 'Red'
        Show-ErrorBox ("以下來源資料夾查不到，請確認磁碟是否已連接、路徑是否打錯，修正 config.ini 後再重新執行：`n`n" +
            ($missingSources -join "`n")) '來源資料夾不存在'
        return
    }

    if (-not (Confirm-CreateFolder -Path $script:stagingFolder -Label '暫存待轉換 (A)')) {
        Write-Log '使用者選擇不建立資料夾，中止。' 'Red'
        return
    }

    if ($Mode -eq 'MoveOnly') {
        Write-Log '開始搬移檔案到暫存資料夾...'
        $moved = Move-EpubsToStaging -SourceFolders $script:sourceFolders -StagingFolder $script:stagingFolder
        Write-Log "搬移完成，共 $moved 個檔案。" 'Green'
        Write-Log '=== 執行完成 ===' 'Navy'
        return
    }

    # --- 以下只有 (a) 移動並轉換 會走到 ---

    Write-Log "錯誤資料夾: $($script:errorFolder)"
    Write-Log "轉換完成 (B): $($script:convertedFolder)"
    if (-not (Confirm-CreateFolder -Path $script:errorFolder -Label '錯誤 (2.5)')) {
        Write-Log '使用者選擇不建立資料夾，中止。' 'Red'
        return
    }
    if (-not (Confirm-CreateFolder -Path $script:convertedFolder -Label '轉換完成 (B)')) {
        Write-Log '使用者選擇不建立資料夾，中止。' 'Red'
        return
    }

    Write-Log '檢查 calibredb.exe...'
    if (-not $script:calibreDbExe -or -not (Test-Path -LiteralPath $script:calibreDbExe)) {
        Write-Log "找不到 calibredb.exe: $($script:calibreDbExe)" 'Red'
        Show-ErrorBox "找不到 calibredb.exe：`n$($script:calibreDbExe)`n請確認 config.ini 裡的路徑正確。" '相依程式缺失'
        return
    }

    Write-Log '檢查 Calibre 書庫 (metadata.db)...'
    if (-not $script:libraryPath -or -not (Test-Path -LiteralPath (Join-Path $script:libraryPath 'metadata.db'))) {
        Write-Log "書庫路徑底下找不到 metadata.db: $($script:libraryPath)" 'Red'
        Show-ErrorBox "Calibre 書庫路徑底下找不到 metadata.db：`n$($script:libraryPath)`n請確認 config.ini 裡的書庫路徑正確。" '相依程式缺失'
        return
    }

    Write-Log '偵測 Python...'
    $pythonCmd = Get-PythonCmd
    if (-not $pythonCmd) {
        Write-Log '找不到可用的 Python。' 'Red'
        Show-ErrorBox '找不到可用的 Python，請先執行 install.ps1。' '相依程式缺失'
        return
    }
    Write-Log "偵測到 Python: $($pythonCmd -join ' ')"

    Write-Log '檢查 opencc 套件...'
    Invoke-Python -PythonCmd $pythonCmd -ExtraArgs @('-c', 'import opencc') 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log 'opencc 套件未安裝。' 'Red'
        Show-ErrorBox 'Python 環境裡沒有 opencc 套件，請先執行 install.ps1。' '相依程式缺失'
        return
    }
    Write-Log 'opencc 套件正常。'

    # 搬檔：來源 -> A（沿用 A 裡任何殘留檔案，一起處理）
    Write-Log '開始搬移來源檔案到暫存資料夾...'
    $moved = Move-EpubsToStaging -SourceFolders $script:sourceFolders -StagingFolder $script:stagingFolder
    Write-Log "搬移完成，共 $moved 個檔案（暫存資料夾裡原有的殘留檔案也會一起處理）。"

    # 清掉資料夾 B 裡殘留的 .tmp：轉換是「寫暫存檔 -> 驗證 -> 原子性改名」，
    # 若上次被強制中斷，會留下沒改名成功的暫存檔。它不會被誤匯入
    # （副檔名不符掃描條件），但不清就會一直累積。
    $staleTmp = @(Get-ChildItem -LiteralPath $script:convertedFolder -Filter '*.tmp' -File -ErrorAction SilentlyContinue)
    if ($staleTmp.Count -gt 0) {
        Write-Log "清理上次中斷殘留的暫存檔 $($staleTmp.Count) 個..."
        foreach ($t in $staleTmp) {
            Remove-Item -LiteralPath $t.FullName -Force -ErrorAction SilentlyContinue
        }
    }

    # 轉換：對 A 裡（含殘留）每個 epub 逐一處理
    $epubFiles = @(Get-ChildItem -LiteralPath $script:stagingFolder -Filter '*.epub' -File -ErrorAction SilentlyContinue)
    Write-Log "暫存資料夾內共 $($epubFiles.Count) 個 epub 待處理。" 'Navy'

    $okCount = 0
    $repairedCount = 0
    $repairedList = New-Object System.Collections.Generic.List[string]
    $skipCount = 0
    $brokenCount = 0
    $failCount = 0
    $errorList = New-Object System.Collections.Generic.List[string]
    $fileIndex = 0

    foreach ($f in $epubFiles) {
        # 只在「兩本書之間」收手，不打斷正在轉換的那一本，避免半成品
        if ($script:cancelRequested) {
            Write-Log ''
            Write-Log "已停止：使用者中止，剩餘 $($epubFiles.Count - $fileIndex) 本未處理。" 'DarkOrange'
            break
        }

        $fileIndex++
        Write-Log "[$fileIndex/$($epubFiles.Count)] $($f.Name)"

        # 外層檔名本身也要簡轉繁，跟書內文字用同一個轉換策略。
        # 呼叫失敗或解析不出來就退回原始檔名，不能因為這個附加功能
        # 讓整個轉換失敗——書名沒轉到頂多不美觀，不影響書能不能讀。
        $stem = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $ext = [IO.Path]::GetExtension($f.Name)
        $outFileName = $f.Name
        $nameRaw = Invoke-Python -PythonCmd $pythonCmd -ExtraArgs @(
            $ConvertScript, 'convert_name', $stem, '-c', $script:openccConfig) 2>&1
        try {
            $nameResult = ($nameRaw | Select-Object -Last 1) | ConvertFrom-Json
            if ($nameResult.status -eq 'ok' -and $nameResult.name) {
                $outFileName = (Get-SanitizedFileName -Name $nameResult.name) + $ext
            }
            else {
                Write-Log "  （檔名轉換未成功，沿用原始檔名。回應: $($nameRaw -join ' | '))" 'DarkOrange'
            }
        }
        catch {
            # 解析失敗也要看得到原始輸出，才能診斷是 python 端出錯、
            # 還是輸出格式跟預期不一樣——之前設計成完全不記錄，等於
            # 自己把診斷痕跡抹掉，這次先補上
            Write-Log "  （檔名轉換解析失敗，沿用原始檔名。原始輸出: $($nameRaw -join ' | '))" 'DarkOrange'
        }
        if ($outFileName -ne $f.Name) {
            Write-Log "  檔名轉換為: $outFileName"
        }

        $outPath = Get-UniqueDestinationPath -DestFolder $script:convertedFolder -FileName $outFileName
        $rawResult = Invoke-Python -PythonCmd $pythonCmd -ExtraArgs @(
            $ConvertScript, 'convert', $f.FullName, $outPath, '-c', $script:openccConfig) 2>&1
        $jsonLine = ($rawResult | Select-Object -Last 1)
        try {
            $result = $jsonLine | ConvertFrom-Json
        }
        catch {
            $result = [PSCustomObject]@{ status = 'error'; reason = "python 輸出無法解析: $jsonLine" }
        }

        switch ($result.status) {
            'ok' {
                if ($result.note) {
                    Write-Log "  -> 成功（$($result.note)）" 'Green'
                }
                else {
                    Write-Log '  -> 成功' 'Green'
                }
                if ($result.repaired) {
                    # 來源 zip 目錄索引損毀但內容完整，轉換時已用標準寫法重寫（等於修好）
                    Write-Log '  -> 來源的 zip 目錄索引原本就損毀（內容完整，已核對），轉換時已一併修復' 'DarkOrange'
                    $repairedCount++
                    $repairedList.Add($f.Name)
                }
                Move-ToRecycleBin -Path $f.FullName
                $okCount++
            }
            'source_broken' {
                # 來源本身就壞了，不是轉換造成的 -> 留在暫存資料夾原地不動，
                # 方便使用者直接打開檢查（錯誤資料夾只放「我們轉壞的」）
                Write-Log "  -> 來源本身損壞，留在暫存資料夾" 'DarkOrange'
                $brokenCount++
                $problemDetail = if ($result.problems) { ' | ' + ($result.problems -join '; ') } else { '' }
                $errorList.Add("[來源本身損壞-留在暫存資料夾] $($f.Name): $($result.reason)$problemDetail")
            }
            'skipped' {
                # 非壞檔（例如偵測為日文書），也不是轉換的責任 -> 同樣留在暫存資料夾
                Write-Log "  -> 跳過: $($result.reason)" 'DarkOrange'
                $skipCount++
                $errorList.Add("[跳過-非壞檔-留在暫存資料夾] $($f.Name): $($result.reason)")
            }
            default {
                # 轉換後才出現的新問題 = 轉換程式造成的損壞 -> 移到錯誤資料夾
                Write-Log "  -> 轉換造成損壞: $($result.reason)" 'Red'
                $dest = Get-UniqueDestinationPath -DestFolder $script:errorFolder -FileName $f.Name
                Move-Item -LiteralPath $f.FullName -Destination $dest
                $failCount++
                $problemDetail = if ($result.problems) { ' | ' + ($result.problems -join '; ') } else { '' }
                $errorList.Add("[轉換造成損壞-已移到錯誤資料夾] $($f.Name): $($result.reason)$problemDetail")
            }
        }
    }

    # 匯入 Calibre：全部轉換完成後才一次匯入，不加 --duplicates，用內建去重。
    # 掃描的是整個資料夾 B，所以上次匯入失敗殘留的檔案這次也會一併補上
    # （B 的定位就是「待匯入佇列」）。
    $importOk = $true
    $importedCount = 0

    if ($script:cancelRequested) {
        # 使用者中止 -> 不匯入，已轉好的檔案留在 B，下次執行時會一起匯入
        $remaining = @(Get-ChildItem -LiteralPath $script:convertedFolder -Filter '*.epub' -File -ErrorAction SilentlyContinue)
        Write-Log ''
        Write-Log "已中止，略過匯入。$($remaining.Count) 個已轉換的檔案保留在轉換完成資料夾，下次執行會一併匯入。" 'DarkOrange'
    }
    else {
        Write-Log '準備匯入 Calibre...' 'Navy'
        $importedFiles = @(Get-ChildItem -LiteralPath $script:convertedFolder -Filter '*.epub' -File -ErrorAction SilentlyContinue)

        if ($importedFiles.Count -gt 0) {
            Write-Log "匯入 $($importedFiles.Count) 個檔案..."
            $importArgs = @('add') + ($importedFiles.FullName) + @('--with-library', $script:libraryPath)
            & $script:calibreDbExe @importArgs 2>&1 | ForEach-Object { Write-Log "  calibredb: $_" 'Gray' }
            if ($LASTEXITCODE -ne 0) {
                Write-Log 'calibredb 匯入失敗，檔案保留在轉換完成資料夾。' 'Red'
                $importOk = $false
            }
            else {
                $importedCount = $importedFiles.Count
                Write-Log '匯入成功，清空轉換完成資料夾...' 'Green'
                foreach ($imported in $importedFiles) {
                    Move-ToRecycleBin -Path $imported.FullName
                }
            }
        }
        else {
            Write-Log '沒有待匯入的檔案。'
        }
    }

    # 總結
    Write-Log ''
    Write-Log "$(if ($script:cancelRequested) { '=== 已中止 ===' } else { '=== 執行完成 ===' })" 'Navy'
    Write-Log "轉換成功: $okCount 本" 'Green'
    if ($repairedCount -gt 0) {
        Write-Log "  其中 $repairedCount 本來源的 zip 目錄索引原本損毀，已在轉換時修復" 'DarkOrange'
    }
    Write-Log "來源本身損壞、未轉換（留在暫存資料夾供檢查）: $brokenCount 本"
    Write-Log "非壞檔但跳過，如日文書（留在暫存資料夾）: $skipCount 本"
    Write-Log "轉換程式造成損壞（已移到錯誤資料夾，代表程式有問題）: $failCount 本"
    Write-Log "Calibre 匯入狀態: $(if ($script:cancelRequested) { '已中止，未匯入' } elseif ($importOk) { '成功' } else { '失敗' })"

    if ($repairedList.Count -gt 0) {
        Write-Log ''
        Write-Log '已修復目錄索引的書（原始檔案損毀，輸出檔案已是正常結構）：' 'Navy'
        foreach ($name in $repairedList) { Write-Log "  $name" }
    }

    if ($errorList.Count -gt 0) {
        Write-Log ''
        Write-Log '問題清單：' 'Navy'
        foreach ($item in $errorList) { Write-Log "  $item" }
    }

    # 只有真的有匯入書才開啟 Calibre
    if ($importOk -and $importedCount -gt 0) {
        if ($script:calibreExe -and (Test-Path -LiteralPath $script:calibreExe)) {
            Write-Log ''
            Write-Log '開啟 Calibre...' 'Navy'
            Start-Process -FilePath $script:calibreExe
        }
        else {
            Write-Log ''
            Write-Log "找不到 Calibre 主程式，略過自動開啟: $($script:calibreExe)" 'DarkOrange'
        }
    }
}

# ---------- 讀取設定 ----------

try {
    $ini = Read-IniConfig -Path $ConfigPath
}
catch {
    # GUI 還沒建立起來，只能用獨立的 MessageBox 呈現
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '設定檔錯誤',
        [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

$paths = $ini['Paths']
$calibre = $ini['Calibre']
$conv = $ini['Conversion']
if (-not $conv) { $conv = @{} }

$script:sourceFolders = Get-SourceFolders -PathsSection $paths
$script:stagingFolder = $paths['StagingFolder']
$script:errorFolder = $paths['ErrorFolder']
$script:convertedFolder = $paths['ConvertedFolder']
$script:calibreDbExe = $calibre['CalibreDbExe']
$script:calibreExe = $calibre['CalibreExe']
$script:libraryPath = $calibre['LibraryPath']
$script:openccConfig = if ($conv.ContainsKey('OpenCCConfig')) { $conv['OpenCCConfig'] } else { 's2twp' }

# 共通檢查：所有設定路徑不可重複，避免檔案搬到自己身上
$allConfigured = @($script:sourceFolders + @($script:stagingFolder, $script:errorFolder, $script:convertedFolder) |
    Where-Object { $_ })
$normalized = $allConfigured | ForEach-Object { $_.TrimEnd('\').ToLower() }
if (($normalized | Select-Object -Unique).Count -ne $normalized.Count) {
    [System.Windows.Forms.MessageBox]::Show(
        'config.ini 裡有重複或重疊的路徑設定，請檢查來源資料夾、暫存資料夾、錯誤資料夾、轉換完成資料夾是否互相撞到。',
        '設定錯誤', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

# ---------- 建立主視窗 ----------

# Windows 桌面應用的版面慣例：外邊距 14px、相關控制項間距 6px、
# 標準命令按鈕 77x25。視窗大小貼合當下階段的內容，不預留大片空白。
$M = 14        # 外邊距
$BTN_W = 77    # 標準按鈕寬
$BTN_H = 25    # 標準按鈕高

function Get-UIFont {
    param([single]$Size = 9)
    # 繁中 Windows 的系統介面字型是微軟正黑體；取不到就退回 Segoe UI
    foreach ($name in @('Microsoft JhengHei UI', 'Segoe UI')) {
        try {
            $f = New-Object System.Drawing.Font($name, $Size)
            if ($f.Name -eq $name) { return $f }
        }
        catch { }
    }
    return New-Object System.Drawing.Font('Segoe UI', $Size)
}

function New-ActionCard {
    # 用標準 WinForms 控制項組出「命令連結」的外觀：粗體標題 + 灰色說明，
    # 滑鼠移入時高亮。
    #
    # 為什麼不用 Windows 原生的 BS_COMMANDLINK：那個樣式必須在控制項建立
    # 的當下就指定，事後用 SetWindowLong 改，Windows 不會重新計算與重繪，
    # 結果是按鈕存在、可以點，但完全不顯示。改用一般控制項自己畫，
    # 外觀一樣、而且保證畫得出來。
    param(
        [string]$Title,
        [string]$Note,
        [int]$X, [int]$Y, [int]$Width, [int]$Height
    )

    $normalBack = [System.Drawing.Color]::White
    $hoverBack = [System.Drawing.Color]::FromArgb(229, 241, 251)

    $card = New-Object System.Windows.Forms.Panel
    $card.Location = New-Object System.Drawing.Point($X, $Y)
    $card.Size = New-Object System.Drawing.Size($Width, $Height)
    $card.BackColor = $normalBack
    $card.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
    $card.Cursor = [System.Windows.Forms.Cursors]::Hand

    $lblT = New-Object System.Windows.Forms.Label
    $lblT.Text = $Title
    $lblT.AutoSize = $true
    $lblT.Location = New-Object System.Drawing.Point(14, 9)
    $lblT.Font = Get-UIFont 10.5
    $lblT.ForeColor = [System.Drawing.Color]::FromArgb(0, 51, 153)
    $lblT.Cursor = [System.Windows.Forms.Cursors]::Hand
    $card.Controls.Add($lblT)

    $lblN = New-Object System.Windows.Forms.Label
    $lblN.Text = $Note
    $lblN.AutoSize = $true
    $lblN.Location = New-Object System.Drawing.Point(16, 33)
    $lblN.Font = Get-UIFont 8.5
    $lblN.ForeColor = [System.Drawing.Color]::FromArgb(90, 90, 90)
    $lblN.Cursor = [System.Windows.Forms.Cursors]::Hand
    $card.Controls.Add($lblN)

    # 點擊與滑鼠移入移出都要涵蓋子控制項，否則游標移到文字上就失效
    $setHover = { $card.BackColor = $hoverBack; $lblT.BackColor = $hoverBack; $lblN.BackColor = $hoverBack }.GetNewClosure()
    $setNormal = { $card.BackColor = $normalBack; $lblT.BackColor = $normalBack; $lblN.BackColor = $normalBack }.GetNewClosure()

    foreach ($ctrl in @($card, $lblT, $lblN)) {
        $ctrl.Add_MouseEnter($setHover)
        $ctrl.Add_MouseLeave($setNormal)
    }

    return $card
}

$script:form = New-Object System.Windows.Forms.Form
$script:form.Text = "EPUB 簡轉繁匯入 Calibre v$($script:ScriptVersion)"
$script:form.StartPosition = 'CenterScreen'
$script:form.FormBorderStyle = 'FixedDialog'
$script:form.MaximizeBox = $false
$script:form.MinimizeBox = $false
$script:form.Font = Get-UIFont 9
$script:form.BackColor = [System.Drawing.SystemColors]::Control
$script:form.ClientSize = New-Object System.Drawing.Size(452, 222)

$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Text = '請選擇要執行的動作：'
$lblTitle.AutoSize = $true
$lblTitle.Location = New-Object System.Drawing.Point($M, $M)
$script:form.Controls.Add($lblTitle)

# --- 選項階段 ---
$panelChoice = New-Object System.Windows.Forms.Panel
$panelChoice.Location = New-Object System.Drawing.Point($M, 40)
$panelChoice.Size = New-Object System.Drawing.Size(424, 130)
$script:form.Controls.Add($panelChoice)

# 卡片的實際點擊行為在後面 $startWorkflow 定義好之後才掛上，
# 這裡先建立外觀，避免前向參照
$cardConvert = New-ActionCard -Title '移動並轉換' `
    -Note '搬移到暫存資料夾、簡轉繁、匯入 Calibre 後自動開啟' `
    -X 0 -Y 0 -Width 424 -Height 58
$panelChoice.Controls.Add($cardConvert)

$cardMoveOnly = New-ActionCard -Title '只移動檔案' `
    -Note '僅把來源的 epub 搬到暫存資料夾，不轉換也不匯入' `
    -X 0 -Y 64 -Width 424 -Height 58
$panelChoice.Controls.Add($cardMoveOnly)

# 取消是次要動作，用標準尺寸按鈕靠右下，不做成命令連結
$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = '取消'
$btnCancel.Size = New-Object System.Drawing.Size($BTN_W, $BTN_H)
$btnCancel.Location = New-Object System.Drawing.Point((452 - $M - $BTN_W), 183)
$script:form.Controls.Add($btnCancel)

# --- 執行階段 ---
$script:logBox = New-Object System.Windows.Forms.RichTextBox
$script:logBox.Location = New-Object System.Drawing.Point($M, 40)
$script:logBox.Size = New-Object System.Drawing.Size(692, 412)
$script:logBox.ReadOnly = $true
$script:logBox.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$script:logBox.BackColor = [System.Drawing.SystemColors]::Window
$script:logBox.Font = Get-UIFont 9
$script:logBox.Visible = $false
$script:form.Controls.Add($script:logBox)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = '關閉'
$btnClose.Size = New-Object System.Drawing.Size($BTN_W, $BTN_H)
$btnClose.Location = New-Object System.Drawing.Point((720 - $M - $BTN_W), 464)
$btnClose.Visible = $false
$btnClose.Add_Click({ $script:form.Close() })
$script:form.Controls.Add($btnClose)

# 停止按鈕：只在轉換進行中顯示。按下後設定旗標，轉換迴圈會在
# 「兩本書之間」的空檔檢查並收手 —— 不會打斷正在轉換的那一本，
# 避免產生半成品（單本轉換很快，實際上不會有明顯延遲）。
$btnStop = New-Object System.Windows.Forms.Button
$btnStop.Text = '停止'
$btnStop.Size = New-Object System.Drawing.Size($BTN_W, $BTN_H)
$btnStop.Location = New-Object System.Drawing.Point((720 - $M - $BTN_W), 464)
$btnStop.Visible = $false
$btnStop.Add_Click({
    $script:cancelRequested = $true
    $btnStop.Enabled = $false
    $btnStop.Text = '停止中…'
})
$script:form.Controls.Add($btnStop)

# Esc 關閉（卡片不是 IButtonControl，Enter 用 KeyPreview 另外處理）
$script:form.CancelButton = $btnCancel

# 切換到執行階段，然後跑流程。工作跑在事件處理常式裡，視窗維持開著，
# 使用者看完記錄再自己按關閉。
$startWorkflow = {
    param([string]$Mode)
    $script:cancelRequested = $false
    $panelChoice.Visible = $false
    $btnCancel.Visible = $false
    $lblTitle.Text = '執行進度：'
    # 進度畫面需要的空間比選單大很多，這時候才放大視窗，
    # 避免選單階段下半部整片空白
    $script:form.ClientSize = New-Object System.Drawing.Size(720, 503)
    $script:form.CenterToScreen()
    $script:logBox.Visible = $true
    $btnStop.Visible = $true
    $btnStop.Enabled = $true
    $btnStop.Text = '停止'
    $script:form.KeyPreview = $false
    $script:form.CancelButton = $null
    $script:form.Refresh()

    try {
        Invoke-Workflow -Mode $Mode
    }
    catch {
        Write-Log ''
        Write-Log '===== 執行過程發生未預期的錯誤 =====' 'Red'
        Write-Log $_.Exception.Message 'Red'
        Write-Log $_.ScriptStackTrace 'Gray'
    }

    $btnStop.Visible = $false
    $lblTitle.Text = '執行完畢，可關閉視窗。'
    $btnClose.Visible = $true
    $script:form.AcceptButton = $btnClose
    $script:form.CancelButton = $btnClose
    $btnClose.Focus()
}

# 卡片的點擊行為（建立時先掛空的，這裡換成真正的動作）
$convertClick = { & $startWorkflow 'Convert' }.GetNewClosure()
$moveOnlyClick = { & $startWorkflow 'MoveOnly' }.GetNewClosure()

foreach ($ctrl in @($cardConvert) + @($cardConvert.Controls)) {
    $ctrl.Add_Click($convertClick)
}
foreach ($ctrl in @($cardMoveOnly) + @($cardMoveOnly.Controls)) {
    $ctrl.Add_Click($moveOnlyClick)
}

$btnCancel.Add_Click({ $script:form.Close() })

# Enter 執行主要動作
$script:form.KeyPreview = $true
$script:form.Add_KeyDown({
    if ($_.KeyCode -eq [System.Windows.Forms.Keys]::Enter -and $panelChoice.Visible) {
        & $startWorkflow 'Convert'
    }
})

[void]$script:form.ShowDialog()
