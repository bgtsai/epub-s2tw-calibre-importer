#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Version: 1.0.1
# ============================================================
"""
EPUB 簡轉繁核心轉換與完整性驗證工具

改寫自 https://github.com/hungte/epub-s2tw（MIT License），原版只轉換文字類內容
（.html/.xhtml/.ncx/.txt/.opf/.css），zip 內的檔名完全不動。

改寫重點（解決原版的已知風險）：
  1. 檔名與內文用「同一個」OpenCC 轉換函式同步處理。
     原因：如果 zip 內圖片等檔名本身含簡體字，原版只轉引用它的文字
     （.opf manifest、.xhtml 的 <img src="...">），檔名本身不轉，
     兩邊就會對不上，造成閱讀器打開時圖片裂圖、目錄連結失效——
     而且完全不會報錯，是靜默失效。
     解法：檔名跟參照文字用同一個轉換器跑，天生同步，不需要另外
     維護一份「舊檔名 -> 新檔名」的對照表去手動修正參照。
  2. META-INF/container.xml 是特例：它記錄 .opf 檔案本身的路徑，
     內容不能整份丟給 OpenCC 轉（那是系統索引，不是給讀者看的敘述），
     但如果 .opf 檔名本身有轉換到，container.xml 裡指向它的路徑
     也要同步更新，否則整本書會直接打不開（比裂圖更嚴重）。
  3. 轉換前後都做一次「連結完整性驗證」，並且比對兩份結果：
     - 轉換前就有致命問題（container.xml 缺失、OPF 不存在、spine 正文
       章節缺檔）-> 判定 source_broken，不轉換，也不搬到錯誤資料夾，
       留在暫存資料夾讓使用者自己打開檢查。
     - 非致命問題（缺圖、CSS url() 失效、nav 目錄失效連結）照樣轉換。
       實務上很多電子書帶著製作工具留下的佔位參照（styles/XXXXXXXX、
       Filename001.xhtml、連號的 images/123.jpeg），書讀起來完全正常，
       不該因此擋下整本。
     - 轉換後只看「新增的問題」：轉換前就存在的問題不算轉換程式的責任；
       只有轉換後才冒出來的問題，才判定為轉換造成的損壞（status: error），
       這種才搬到錯誤資料夾，代表程式有 bug 要修。
     比對時會先把轉換前的問題訊息也套一次同樣的 OpenCC 轉換，因為訊息
     裡含檔名、而檔名本身會被轉換，否則同一個問題會被誤判成新問題。
  4. 日文假名密度防誤判：偵測書籍整體平假名/片假名密度，超過門檻
     視為日文為主的內容，整本跳過轉換（避免破壞日文原文書或內含
     大量日文引用的書）。跟既有字幕轉換腳本用的是同一套判斷邏輯
     與門檻值（15%），只是判斷單位從「單一字幕檔」改成「整本書」。

用法（由 run.ps1 呼叫，結果印一行 JSON 到 stdout 供 PowerShell 解析）：
  python convert_epub.py check <input.epub>
  python convert_epub.py convert <input.epub> <output.epub> [-c s2twp]

退出碼：0 = 成功或跳過（status: ok / skipped），1 = 失敗（status: error）
"""

import sys
import os
import json
import re
import zipfile
import posixpath
from urllib.parse import unquote, urlparse

# 對照 run.ps1 視窗標題的版本號，確認三個檔案抓到的是同一批
SCRIPT_VERSION = '1.0.1'

try:
    from opencc import OpenCC
except ImportError:
    OpenCC = None

KANA_THRESHOLD = 0.15
CONVERT_EXTS = ('.html', '.xhtml', '.htm', '.ncx', '.txt', '.opf', '.css')
SKIP_SCHEMES = ('http', 'https', 'mailto', 'tel', 'data')

HREF_SRC_PATTERN = re.compile(r'\b(?:href|src)\s*=\s*"([^"]*)"', re.IGNORECASE)
URL_FUNC_PATTERN = re.compile(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', re.IGNORECASE)
ITEM_HREF_PATTERN = re.compile(r'<item\b[^>]*\bhref="([^"]+)"', re.IGNORECASE)
MANIFEST_ITEM_TAG_PATTERN = re.compile(r'<item\b[^>]*/?>', re.IGNORECASE)
ATTR_ID_PATTERN = re.compile(r'\bid="([^"]*)"', re.IGNORECASE)
ATTR_HREF_PATTERN = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
SPINE_ITEMREF_PATTERN = re.compile(r'<itemref\b[^>]*\bidref="([^"]+)"', re.IGNORECASE)
NCX_SRC_PATTERN = re.compile(r'<content\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
ROOTFILE_PATTERN = re.compile(r'<rootfile\b[^>]*\bfull-path="([^"]+)"', re.IGNORECASE)

KANA_PATTERN = re.compile(r'[\u3040-\u30FF]')
HAN_PATTERN = re.compile(r'[\u4E00-\u9FFF]')

TEXT_SCAN_EXTS = ('.xhtml', '.html', '.htm', '.ncx', '.css')


def resolve_ref(base_dir, ref):
    """把 href/src 裡的相對路徑，解析成相對於 zip 根目錄的實際路徑。
    絕對 URL（http/mailto 等）與純錨點（#xxx）回傳 None，代表不用檢查。"""
    if not ref:
        return None
    ref = ref.split('#', 1)[0]
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.scheme.lower() in SKIP_SCHEMES:
        return None
    ref = unquote(ref)
    path = posixpath.normpath(posixpath.join(base_dir, ref))
    return path


def parse_manifest(opf_content):
    """從 OPF 取出 manifest 的 id -> href 對照。
    屬性順序在不同製作工具產出的 OPF 裡並不固定（href 可能在 id 前面），
    所以先抓出整個 <item> 標籤、再各自取屬性，不要寫死順序。"""
    manifest = {}
    for tag in MANIFEST_ITEM_TAG_PATTERN.findall(opf_content):
        id_match = ATTR_ID_PATTERN.search(tag)
        href_match = ATTR_HREF_PATTERN.search(tag)
        if id_match and href_match:
            manifest[id_match.group(1)] = href_match.group(1)
    return manifest


def check_integrity(zf):
    """驗證 epub 內部連結完整性。

    回傳 (fatal: list[str], nonfatal: list[str])。

    致命／非致命的分界，看的是「這本書還能不能讀」：
      致命 —— 缺 container.xml、OPF 不存在、spine（正文章節順序）裡列到的
              內容檔不存在。這幾種會讓閱讀器根本打不開書或直接缺章節。
      非致命 —— 圖片參照不到、CSS url() 指不到、目錄／nav 裡的失效連結、
              manifest 裡非 spine 項目的參照。實務上很多電子書製作工具會
              留下範本佔位字串（例如 styles/XXXXXXXX、Filename001.xhtml、
              連號的 images/123.jpeg），書本身讀起來完全正常，只是有沒清
              乾淨的垃圾參照——這種不該擋下整本書。
    """
    fatal = []
    nonfatal = []
    names = set(zf.namelist())

    if 'META-INF/container.xml' not in names:
        return ['缺少 META-INF/container.xml，不是合法的 EPUB'], []

    container = zf.read('META-INF/container.xml').decode('utf-8', errors='replace')
    rootfiles = ROOTFILE_PATTERN.findall(container)
    if not rootfiles:
        return ['container.xml 裡找不到 rootfile full-path，無法定位 OPF'], []

    for opf_path in rootfiles:
        if opf_path not in names:
            fatal.append(f'container.xml 指向的 OPF 不存在: {opf_path}')
            continue

        opf_dir = posixpath.dirname(opf_path)
        opf_content = zf.read(opf_path).decode('utf-8', errors='replace')

        # 先建立 manifest 的 id -> href 對照，再看 spine 用到哪些 id，
        # 才能判斷某個缺檔是「正文缺章節」（致命）還是「附屬資源缺失」（非致命）
        manifest = parse_manifest(opf_content)
        spine_ids = set(SPINE_ITEMREF_PATTERN.findall(opf_content))

        for item_id, href in manifest.items():
            resolved = resolve_ref(opf_dir, href)
            if resolved and resolved not in names:
                msg = f'OPF manifest 參照不存在: {href}（解析為 {resolved}）'
                if item_id in spine_ids:
                    fatal.append('正文章節缺失 - ' + msg)
                else:
                    nonfatal.append(msg)

    # 逐一檢查所有文字類內容檔內部的 href / src / url()
    # 這些都歸類為非致命：缺圖、失效目錄連結不影響整本書能不能讀
    for name in names:
        if not name.lower().endswith(TEXT_SCAN_EXTS):
            continue
        try:
            content = zf.read(name).decode('utf-8', errors='replace')
        except Exception:
            continue
        base_dir = posixpath.dirname(name)

        refs = list(HREF_SRC_PATTERN.findall(content))
        refs += NCX_SRC_PATTERN.findall(content)
        if name.lower().endswith('.css'):
            refs += URL_FUNC_PATTERN.findall(content)

        for ref in refs:
            resolved = resolve_ref(base_dir, ref)
            if resolved and resolved not in names:
                nonfatal.append(f'{name} 內部參照不存在: {ref}（解析為 {resolved}）')

    return fatal, nonfatal


def compute_kana_density(zf):
    """整本書層級的日文假名密度 = 假名字元數 / (假名 + 漢字字元數)。"""
    kana = 0
    han = 0
    for name in zf.namelist():
        if not name.lower().endswith(('.xhtml', '.html', '.htm', '.txt')):
            continue
        try:
            text = zf.read(name).decode('utf-8', errors='ignore')
        except Exception:
            continue
        kana += len(KANA_PATTERN.findall(text))
        han += len(HAN_PATTERN.findall(text))
    total = kana + han
    return (kana / total) if total else 0.0


def convert_epub(input_path, output_path, cc_config='s2twp'):
    if OpenCC is None:
        return {'status': 'error', 'reason': 'opencc 套件未安裝，請先執行 install.ps1'}

    with zipfile.ZipFile(input_path, 'r') as zf:
        pre_fatal, pre_nonfatal = check_integrity(zf)
        if pre_fatal:
            # 來源本身就壞到不能讀，不是轉換造成的 —— 不搬到錯誤資料夾，
            # 留在暫存資料夾讓使用者自己打開檢查（責任歸屬用資料夾位置區分）
            return {'status': 'source_broken',
                    'reason': '來源檔案本身結構損壞（轉換前檢查即不通過）',
                    'problems': pre_fatal}

        density = compute_kana_density(zf)
        if density > KANA_THRESHOLD:
            return {'status': 'skipped',
                     'reason': f'偵測為日文為主內容（假名密度 {density:.1%} > 門檻 {KANA_THRESHOLD:.0%}），跳過轉換'}

        converter = OpenCC(cc_config)

        # 第一階段：算出每個 zip entry 轉換後的新檔名。
        # 檔名跟內文共用同一個轉換器，兩者天生同步，不需要額外對照表。
        name_map = {}
        for item in zf.infolist():
            name_map[item.filename] = converter.convert(item.filename)

        # 檔名轉換後如果撞名（兩個不同原始檔名轉出同一個新檔名），
        # 直接視為失敗，不要硬寫、覆蓋掉另一個檔案。
        seen = {}
        collisions = []
        for old, new in name_map.items():
            if new in seen and seen[new] != old:
                collisions.append(f'{old} 與 {seen[new]} 轉換後撞名: {new}')
            seen[new] = old
        if collisions:
            return {'status': 'error', 'reason': '檔名轉換後發生撞名，未執行轉換', 'problems': collisions}

        tmp_output = output_path + '.tmp'
        with zipfile.ZipFile(tmp_output, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zf.infolist():
                data = zf.read(item.filename)
                new_name = name_map[item.filename]

                if item.filename == 'META-INF/container.xml':
                    # 特例：只更新 rootfile 指向的路徑文字，不整份丟去轉換
                    text = data.decode('utf-8', errors='replace')

                    def _sub_rootfile(m):
                        old_path = m.group(1)
                        new_path = name_map.get(old_path, old_path)
                        return m.group(0).replace(old_path, new_path)

                    text = ROOTFILE_PATTERN.sub(_sub_rootfile, text)
                    data = text.encode('utf-8')
                elif item.filename.lower().endswith(CONVERT_EXTS):
                    try:
                        text = data.decode('utf-8')
                        data = converter.convert(text).encode('utf-8')
                    except UnicodeDecodeError:
                        pass  # 解不了就當二進位檔原樣保留，不要讓整批失敗

                zi = zipfile.ZipInfo(new_name, date_time=item.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = item.external_attr
                zi.flag_bits |= 0x800  # 標記檔名為 UTF-8，避免中文檔名在部分閱讀器顯示異常
                zout.writestr(zi, data)

    # 轉換完成，再驗證一次，並跟轉換前的問題清單比對。
    # 只有「轉換後才出現、轉換前沒有」的問題，才算是轉換程式的責任。
    # 本來就存在的瑕疵（製作工具留下的佔位參照之類）照樣放行，不能怪轉換程式。
    with zipfile.ZipFile(tmp_output, 'r') as zf2:
        post_fatal, post_nonfatal = check_integrity(zf2)

    # 轉換前的問題訊息裡含有檔名，而檔名本身也會被轉換（簡→繁），
    # 直接用字串比對會把「同一個問題」誤判成新問題，所以先把轉換前的
    # 問題訊息也套一次同樣的轉換，兩邊才在同一個基準上比較。
    pre_all = set(converter.convert(p) for p in (pre_fatal + pre_nonfatal))
    new_problems = [p for p in (post_fatal + post_nonfatal) if p not in pre_all]

    if new_problems:
        os.remove(tmp_output)
        return {'status': 'error',
                'reason': '轉換後出現轉換前沒有的新問題（轉換程式造成的損壞），已中止並移除輸出檔案',
                'problems': new_problems}

    os.replace(tmp_output, output_path)
    result = {'status': 'ok'}
    if pre_nonfatal:
        result['note'] = f'來源本身有 {len(pre_nonfatal)} 處非致命的失效參照（不影響閱讀，已原樣保留）'
    return result


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'status': 'error', 'reason': '參數不足'}, ensure_ascii=False))
        sys.exit(1)

    mode = sys.argv[1]

    if mode != 'version' and len(sys.argv) < 3:
        print(json.dumps({'status': 'error', 'reason': '參數不足'}, ensure_ascii=False))
        sys.exit(1)

    if mode == 'check':
        input_path = sys.argv[2]
        try:
            with zipfile.ZipFile(input_path, 'r') as zf:
                fatal, nonfatal = check_integrity(zf)
        except zipfile.BadZipFile:
            fatal, nonfatal = ['不是合法的 zip / epub 檔案（可能已損毀）'], []
        if fatal:
            result = {'status': 'source_broken', 'reason': '來源檔案本身結構損壞',
                      'problems': fatal, 'nonfatal': nonfatal}
        else:
            result = {'status': 'ok', 'nonfatal': nonfatal}

    elif mode == 'convert':
        input_path = sys.argv[2]
        output_path = sys.argv[3]
        cc_config = 's2twp'
        if '-c' in sys.argv:
            cc_config = sys.argv[sys.argv.index('-c') + 1]
        try:
            result = convert_epub(input_path, output_path, cc_config)
        except zipfile.BadZipFile:
            result = {'status': 'error', 'reason': '不是合法的 zip / epub 檔案（可能已損毀）'}

    elif mode == 'convert_name':
        # 只轉換一段文字（外層檔名主體，不含副檔名），不碰任何檔案。
        # 用同一個 OpenCC 設定檔，確保檔名跟書內文字的簡繁轉換策略一致。
        if OpenCC is None:
            result = {'status': 'error', 'reason': 'opencc 套件未安裝'}
        else:
            text = sys.argv[2]
            cc_config = 's2twp'
            if '-c' in sys.argv:
                cc_config = sys.argv[sys.argv.index('-c') + 1]
            converter = OpenCC(cc_config)
            result = {'status': 'ok', 'name': converter.convert(text)}

    elif mode == 'version':
        result = {'status': 'ok', 'version': SCRIPT_VERSION}

    else:
        result = {'status': 'error', 'reason': f'未知模式: {mode}'}

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get('status') in ('ok', 'skipped', 'source_broken') else 1)


if __name__ == '__main__':
    main()
