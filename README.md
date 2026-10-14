# epub-s2tw-calibre-importer

Windows 一鍵工具:把簡體中文 EPUB 轉成台灣正體(OpenCC `s2twp`),**檔名與內文同步轉換**,
轉換前後驗證連結完整性,最後用 `calibredb` 自動匯入 Calibre 書庫。

**English summary:** A one-click Windows tool (PowerShell WinForms GUI + Python) that moves EPUBs from
your download folders, converts Simplified Chinese to Traditional Chinese (Taiwan standard, via OpenCC `s2twp`)
in both file names and book contents, verifies internal link integrity before/after, and imports the results
into Calibre with `calibredb`. The UI and documentation are in Traditional Chinese.

## 特色

- 檔名與內文用同一個轉換器同步轉換,避免 EPUB 內部檔名參照斷裂
- 轉換前檢查來源是否損壞、轉換後比對前後差異,分清「書本來就壞」與「轉換造成的問題」
- 日文書偵測(假名密度),避免誤轉
- 自動修復損毀的 zip 目錄索引：有些 EPUB 內容完整、只是檔尾索引寫壞，Calibre 能開但嚴格工具會拒絕；程式會從本地檔頭逐一取出、核對 CRC，全部正確就在轉換時順便重寫修好（任何檔案核對失敗仍判定為來源損壞，不硬修）
- 輸出遵循 EPUB 規範：`mimetype` 固定第一個、不壓縮
- 原子性寫入(暫存檔 → 改名),中斷也不會留下半成品;來源檔全程唯讀
- 單一視窗、即時進度、可中途停止;有書匯入時結束後自動開啟 Calibre

## 系統需求

- Windows 10 / 11(PowerShell 5.1,系統內建)
- [Calibre](https://calibre-ebook.com/)(使用其命令列工具 `calibredb`)
- Python 3 與 `opencc-python-reimplemented`(`install.ps1` 會自動安裝)

## 快速開始

1. 到 [Releases](https://github.com/bgtsai/epub-s2tw-calibre-importer/releases) 下載最新的 zip,解壓到你想放的位置。
2. 若檔案是從網路下載的,先在每個 `.ps1` 上按右鍵 → 內容 → 勾選「解除封鎖」。
3. 以系統管理員身分執行 `install.ps1`(首次安裝,見下節)。安裝最後會自動建立 `config.ini` 並用記事本開啟,把 `<你的路徑>` 改成你的資料夾後存檔。
4. 之後點捷徑或執行 `run.ps1` 即可。

## 更新到新版

下載新版 zip,解壓後**整包覆蓋**到原本的資料夾即可。發布包裡只有範本 `config.example.ini`,
不含 `config.ini`,所以你的設定不會被蓋掉。不需要重新執行 `install.ps1`
(除非新版說明有特別註明)。視窗標題列會顯示版本號,可以確認是否更新成功。

## 首次安裝

以系統管理員身分執行 `install.ps1`（未以系統管理員身分執行會自動重啟一次要求權限）：
- 沒裝 Python 會自動下載官方安裝程式並靜默安裝（全系統）
- 沒裝 `opencc-python-reimplemented` 會自動 `pip install`
- 在本資料夾產生一個捷徑 `執行_EPUB簡轉繁匯入Calibre.lnk`，可自行搬到桌面或其他位置

重複執行是安全的（已裝過的部分會跳過）。

## 使用前設定

`install.ps1` 安裝完成時(或第一次執行 `run.ps1` 時)會從範本 `config.example.ini` 自動建立 `config.ini` 並用記事本開啟,把裡面的 `<你的路徑>` 改成你自己的資料夾、存檔即可。設定還沒改完就執行,程式會列出還沒改的項目並重新開啟記事本。

**請修改 `config.ini`,不要改 `config.example.ini`**——範本在更新時會被覆蓋,`config.ini` 不在發布包裡,更新不會動到它。
檔案內每個設定都有中文註解說明用途,主要有:
- `SourceFolder1`(可依序加 `SourceFolder2`、`SourceFolder3` ... 支援多個來源)
- `StagingFolder`(暫存待轉換,即文件裡的資料夾 A)
- `ErrorFolder`(轉換造成問題的檔案會移到這裡)
- `ConvertedFolder`(轉換成功、等待匯入 Calibre 的資料夾,即資料夾 B)
- `CalibreDbExe`(命令列工具)、`CalibreExe`(GUI 主程式)、`LibraryPath`(書庫)

## 日常使用

執行 `run.ps1`（或桌面上的捷徑），會開啟單一視窗，裡面有三個選項：

- **(a) 移動並轉換**：來源 → 暫存 A → 逐一驗證＋轉換 → 輸出到 B → 匯入 Calibre → 清空 B
- **(b) 只移動檔案**：只把來源資料夾的 epub 搬到 A，不做轉換與匯入
- **(c) 取消**

選擇之後，同一個視窗會切換成即時進度畫面，跑完直接在下方顯示總結。
真的有書匯入時，結束後會自動開啟 Calibre。

轉換進行中右下角有「停止」按鈕。停止只在**兩本書之間**生效，不會打斷
正在轉換的那一本（避免產生半成品）。按下停止後不會匯入 Calibre，已經
轉好的檔案留在資料夾 B，下次執行時會一併匯入。

## 執行記錄與 Calibre

- 每次執行的 Log 會存到程式資料夾的 `logs\`(檔名為執行時間),只保留最近 30 份,方便事後核對。
- 匯入前如果 Calibre 正開著,會先提示關閉。Calibre 開著時 `calibredb` 無法安全寫入同一個書庫;
  選「取消」則略過匯入,檔案留到下次。
- 若 Calibre 設定了「自動新增」(偏好設定 → 新增書籍 → 自動新增)監看轉換完成資料夾,
  Calibre 會自己取走檔案,建議清空該設定,交給本程式匯入。

## 中斷安全性

來源檔案全程唯讀，不會被就地改寫。轉換的產出先寫成暫存檔（`.epub.tmp`），
通過完整性驗證後才用原子性改名換成正式檔名——所以資料夾 B 裡不會出現
「看起來正常、其實只轉一半」的檔案。若程式被強制中斷，來源檔完好留在
資料夾 A，下次執行重新轉換即可；殘留的 `.epub.tmp` 會在下次執行開始時自動清除。

## 檔案去向代表責任歸屬

| 結果 | 檔案去向 |
| --- | --- |
| 轉換成功 | 來源進資源回收筒，產出進資料夾 B 並匯入 Calibre |
| 來源 zip 目錄索引損毀，但內容完整 | 轉換時自動修復，視為成功；摘要會列出這些書 |
| 來源本身損壞（含資料毀損、無法救回） | 留在暫存資料夾 A 原地不動，方便直接打開檢查 |
| 非壞檔但跳過（如日文書） | 同上，留在 A |
| 轉換造成的損壞 | 移到錯誤資料夾（代表這支程式有 bug 要修） |

「來源本身損壞」只認三種致命問題：`container.xml` 缺失、OPF 不存在、
spine 正文章節缺檔。缺圖、CSS `url()` 失效、目錄失效連結都算非致命，
照樣轉換——很多電子書帶著製作工具留下的佔位參照，書讀起來完全正常。

## 版本號

`run.ps1`、`install.ps1`、`convert_epub.py` 三個檔案各自的版本號寫在**檔案最前面**
（`.ps1` 是第 2 行，`.py` 在 shebang／編碼宣告之後），開檔就看得到，不用執行。

**任何改動，不管多小，都要跳版本號**——這樣下載後只要看一眼檔案開頭，
就能確認抓到的是不是最新版，不用執行過一次才知道。

## 已知限制

- `config.ini` 的多來源資料夾用 `SourceFolder1`／`SourceFolder2`／... 逐行寫，不是逗號分隔
- Python 安裝版本寫死在 `install.ps1` 開頭的 `$PythonInstallerVersion`，之後要換版本要手動改
- 連結完整性驗證用正規表示式掃描 href/src/url()，不是完整的 XML/EPUB 規範解析器，
  對於極端不規範的 EPUB 結構可能有漏網之魚（但比原版 epub-s2tw 完全不驗證已經穩妥很多）

## 致謝與引用

- **[epub-s2tw](https://github.com/hungte/epub-s2tw)**(作者 Hung-Te Lin (piaip),MIT License):本專案的 EPUB 轉換邏輯改寫自它,
  並新增檔名同步轉換、前後連結完整性驗證、日文假名防誤判與 Calibre 自動匯入。
- **[OpenCC](https://github.com/BYVoid/OpenCC)**(BYVoid 等人,Apache-2.0):簡繁轉換的詞庫與演算法來源。
- **[opencc-python-reimplemented](https://github.com/yichen0831/opencc-python)**(Apache-2.0):本專案實際呼叫的純 Python 實作,沿用 OpenCC 詞庫。
- **[Calibre](https://calibre-ebook.com/)**(Kovid Goyal,GPLv3):書庫管理,本專案只呼叫其命令列工具 `calibredb`,不包含其程式碼。

## 授權

本專案以 [MIT License](LICENSE) 釋出。改寫自 epub-s2tw 的部分,原專案同為 MIT License,版權屬原作者 Hung-Te Lin (piaip),其版權聲明已併入 [LICENSE](LICENSE)。
