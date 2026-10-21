#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Version: 1.0.6
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

v1.0.2 新增「zip 目錄索引損毀自動修復」：有些製作工具產出的 EPUB，檔案內容完整，
只是檔尾的 central directory 寫壞（例如每筆記錄少 2 個位元組）。Calibre 這類寬鬆
閱讀器能開，但 Python zipfile／unzip 會直接拒絕。遇到這種情況，改掃檔案開頭的
本地檔頭逐一取出內容，每個檔案都核對 CRC 與大小，全數通過才算「可修復」；
轉換時以標準寫法重寫 zip，目錄索引就順便修好了。任何一個檔案核對失敗，或本地檔頭
也讀不通，仍判定為來源損壞（source_broken），不硬修。來源檔案始終唯讀。
另外輸出時 mimetype 固定第一個、不壓縮（EPUB OCF 規範）。

退出碼：0 = 成功或跳過（status: ok / skipped），1 = 失敗（status: error）
"""

import sys
import os
import json
import re
import zipfile
import zlib
import struct
import posixpath
from urllib.parse import unquote, urlparse, quote

# 對照 run.ps1 視窗標題的版本號，確認三個檔案抓到的是同一批
SCRIPT_VERSION = '1.0.6'

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


class EpubSourceError(Exception):
    """來源檔案本身讀不了（不是轉換程式造成的）。"""


class _Entry:
    """寬鬆讀取器用的最小 zip entry，欄位名稱對齊 zipfile.ZipInfo 用到的那幾個。"""
    __slots__ = ('filename', 'date_time', 'external_attr', 'method', 'flags',
                 'crc', 'csize', 'usize', 'data_start')


class LenientZip:
    """目錄索引壞掉時的備援讀取器：不看 central directory，
    從檔案開頭依序走過每個本地檔頭（PK\\x03\\x04）取出內容。

    只在「所有檔案都解得開、CRC 與大小全數相符、且走到的終點是 central directory
    或 EOCD 或剛好檔尾」時才成立，否則丟 EpubSourceError。
    提供 namelist() / infolist() / read()，跟 check_integrity 等函式用的介面一致。"""

    repaired = True

    def __init__(self, path):
        with open(path, 'rb') as f:
            self._buf = f.read()
        self._entries = []
        self._by_name = {}
        self._scan()

    def _scan(self):
        b = self._buf
        p = 0
        if b[:4] != b'PK\x03\x04':
            raise EpubSourceError('檔案開頭不是 zip 格式，可能不是 EPUB 或檔案已嚴重損毀')
        while b[p:p + 4] == b'PK\x03\x04':
            if p + 30 > len(b):
                raise EpubSourceError('本地檔頭被截斷')
            (_ver, flags, method, mtime, mdate, crc, csize, usize,
             fnl, exl) = struct.unpack('<HHHHHIIIHH', b[p + 4:p + 30])
            if flags & 0x1:
                raise EpubSourceError('含加密檔案，無法讀取')
            if flags & 0x8:
                raise EpubSourceError('本地檔頭沒有大小資訊（data descriptor），無法安全修復')
            if method not in (0, 8):
                raise EpubSourceError(f'不支援的壓縮方式 {method}')
            name_start = p + 30
            data_start = name_start + fnl + exl
            data_end = data_start + csize
            if data_end > len(b):
                raise EpubSourceError('檔案內容被截斷')
            raw_name = b[name_start:name_start + fnl]
            name = raw_name.decode('utf-8' if flags & 0x800 else 'cp437', errors='replace')
            e = _Entry()
            e.filename = name
            e.date_time = ((mdate >> 9) + 1980, (mdate >> 5) & 0xF or 1, mdate & 0x1F or 1,
                           mtime >> 11, (mtime >> 5) & 0x3F, (mtime & 0x1F) * 2)
            e.external_attr = 0
            e.method, e.flags, e.crc, e.csize, e.usize = method, flags, crc, csize, usize
            e.data_start = data_start
            # 每個檔案都先完整解開並核對，通過才收進來
            data = self._decode(e)
            if (zlib.crc32(data) & 0xFFFFFFFF) != crc or len(data) != usize:
                raise EpubSourceError(f'檔案內容與記錄的 CRC／大小不符: {name}')
            self._entries.append(e)
            self._by_name[name] = e
            p = data_end
        tail = b[p:p + 4]
        if not (tail in (b'PK\x01\x02', b'PK\x05\x06') or p == len(b)):
            raise EpubSourceError('本地檔頭之後出現無法辨識的資料，可能缺少檔案')
        if not self._entries:
            raise EpubSourceError('找不到任何檔案')

    def _decode(self, e):
        raw = self._buf[e.data_start:e.data_start + e.csize]
        if e.method == 0:
            return raw
        try:
            return zlib.decompress(raw, -15)
        except zlib.error as ex:
            raise EpubSourceError(f'解壓縮失敗: {e.filename}（{ex}）')

    def namelist(self):
        return [e.filename for e in self._entries]

    def infolist(self):
        return list(self._entries)

    def read(self, name):
        return self._decode(self._by_name[name])

    def close(self):
        self._buf = b''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_epub(path):
    """開啟來源 epub：先用標準 zipfile，目錄索引壞掉才退回寬鬆讀取。
    回傳物件有 repaired 屬性（True = 來源目錄索引損毀，已用寬鬆模式讀出）。
    兩種方式都失敗丟 EpubSourceError。"""
    try:
        zf = zipfile.ZipFile(path, 'r')
        zf.repaired = False
        return zf
    except zipfile.BadZipFile:
        pass
    try:
        return LenientZip(path)
    except EpubSourceError as ex:
        raise EpubSourceError(f'不是合法的 zip / epub 檔案，且無法從本地檔頭救回內容（{ex}）')


# 參照路徑若用網址編碼（%XX）寫中文，OpenCC 看到的只是英數字與百分號，不會轉換；
# 但 zip 內的實際檔名會被轉成繁體，兩邊就對不上（v1.0.6 修正的案例：
# OPF 寫 Images/%E5%85%AC%E4%BC%97...jpg，實際檔案改名成 公眾號二維碼3.jpg）。
# 做法：解碼 -> 用同一個轉換器轉 -> 有變化才重新編碼寫回，不含中文的編碼（如 %20）原樣不動。
ENCODED_REF_PATTERN = re.compile(r'((?:\bhref|\bsrc)\s*=\s*")([^"]*%[0-9A-Fa-f]{2}[^"]*)(")', re.IGNORECASE)
ENCODED_URL_FUNC_PATTERN = re.compile(r'(url\(\s*[\'"]?)([^\'")]*%[0-9A-Fa-f]{2}[^\'")]*)([\'"]?\s*\))', re.IGNORECASE)
URL_SAFE_CHARS = "/#?&=:;@!$'()*+,~-._"


def convert_encoded_refs(text, converter):
    def _fix(m):
        raw = m.group(2)
        decoded = unquote(raw)
        converted = converter.convert(decoded)
        if converted == decoded:
            return m.group(0)
        return m.group(1) + quote(converted, safe=URL_SAFE_CHARS) + m.group(3)
    text = ENCODED_REF_PATTERN.sub(_fix, text)
    return ENCODED_URL_FUNC_PATTERN.sub(_fix, text)


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


# ---------- 給人看的損壞說明（v1.0.4） ----------
# check_integrity 回傳的是技術訊息（檔名、解析路徑），這裡另外產生白話說明給 Log 用：
# 缺正文時查書內目錄找出章節標題與閱讀順序位置，讓使用者不用自己拆檔就知道壞在哪。

NAV_LINK_PATTERN = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
NCX_POINT_PATTERN = re.compile(r'<navLabel>\s*<text>(.*?)</text>\s*</navLabel>\s*<content\b[^>]*\bsrc="([^"]+)"',
                               re.IGNORECASE | re.DOTALL)
TAG_PATTERN = re.compile(r'<[^>]+>')


def _clean_title(raw):
    text = TAG_PATTERN.sub('', raw)
    text = re.sub(r'\s+', ' ', text.replace('\u3000', ' ')).strip()
    return text[:60]


def _toc_titles(zf, names):
    """從 nav（.xhtml/.html）與 toc.ncx 收集「檔案路徑 -> 第一個標題」。查不到就是空 dict。"""
    titles = {}
    for name in names:
        low = name.lower()
        try:
            if low.endswith('.ncx'):
                content = zf.read(name).decode('utf-8', errors='replace')
                pairs = [(src, t) for t, src in NCX_POINT_PATTERN.findall(content)]
            elif low.endswith(('.xhtml', '.html', '.htm')) and ('toc' in low or 'nav' in low):
                content = zf.read(name).decode('utf-8', errors='replace')
                pairs = NAV_LINK_PATTERN.findall(content)
            else:
                continue
        except Exception:
            continue
        base_dir = posixpath.dirname(name)
        for href, raw_title in pairs:
            resolved = resolve_ref(base_dir, href)
            title = _clean_title(raw_title)
            if resolved and title and resolved not in titles:
                titles[resolved] = title
    return titles


def describe_source_problems(zf, fatal, nonfatal):
    """把致命問題翻成白話，回傳 list[str]。任何解析失敗都退回原始訊息，不能讓說明本身出錯。"""
    details = []
    try:
        names = set(zf.namelist())
        if 'META-INF/container.xml' not in names:
            return ['不是合法的 EPUB：缺少 META-INF/container.xml（書的入口設定檔）']
        container = zf.read('META-INF/container.xml').decode('utf-8', errors='replace')
        rootfiles = ROOTFILE_PATTERN.findall(container)
        if not rootfiles:
            return ['不是合法的 EPUB：container.xml 沒有指出書的主設定檔（OPF）在哪裡']
        titles = None
        missing_chapters = 0
        for opf_path in rootfiles:
            if opf_path not in names:
                details.append(f'找不到書的主設定檔（OPF）：{opf_path}')
                continue
            opf_dir = posixpath.dirname(opf_path)
            opf_content = zf.read(opf_path).decode('utf-8', errors='replace')
            manifest = parse_manifest(opf_content)
            spine = SPINE_ITEMREF_PATTERN.findall(opf_content)
            for pos, item_id in enumerate(spine, 1):
                href = manifest.get(item_id)
                if not href:
                    continue
                resolved = resolve_ref(opf_dir, href)
                if resolved and resolved not in names:
                    if titles is None:
                        titles = _toc_titles(zf, names)
                    title = titles.get(resolved)
                    label = f'「{title}」' if title else '（目錄裡查不到標題）'
                    details.append(f'缺少正文{label}：閱讀順序第 {pos} 項（共 {len(spine)} 項），檔案 {href} 不存在')
                    missing_chapters += 1
        if missing_chapters:
            details.append('提示：Calibre 等閱讀器仍打得開這本書，但會直接跳過缺少的部分')
    except Exception:
        details = []
    if not details:
        details = list(fatal)
    if nonfatal:
        details.append(f'另有 {len(nonfatal)} 處非致命的失效參照（缺圖、目錄連結等，不影響判定，完整清單見最後的問題清單）')
    return details


def describe_open_error(message):
    """開檔失敗（含寬鬆讀取也救不回來）的白話說明。"""
    if '截斷' in message:
        return ['檔案被截斷，可能下載不完整']
    if 'CRC' in message or '解壓縮失敗' in message:
        m = re.search(r'(?:解壓縮失敗|CRC／大小不符): ([^（）]+)', message)
        where = f'：{m.group(1).strip()}' if m else ''
        return [f'壓縮檔內的資料已毀損{where}，無法安全修復']
    if '開頭不是 zip' in message:
        return ['不是 EPUB／zip 檔案，或檔案已嚴重損毀（可能下載到錯誤頁面或檔案不完整）']
    if '加密' in message:
        return ['檔案含加密內容（可能有 DRM），無法讀取']
    return [message or '不是合法的 zip / epub 檔案（可能已損毀）']


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

    with open_epub(input_path) as zf:
        repaired = bool(getattr(zf, 'repaired', False))
        pre_fatal, pre_nonfatal = check_integrity(zf)
        if pre_fatal:
            # 來源本身就壞到不能讀，不是轉換造成的 —— 不搬到錯誤資料夾，
            # 留在暫存資料夾讓使用者自己打開檢查（責任歸屬用資料夾位置區分）
            return {'status': 'source_broken',
                    'reason': '來源檔案本身結構損壞（轉換前檢查即不通過）',
                    'problems': pre_fatal,
                    'details': describe_source_problems(zf, pre_fatal, pre_nonfatal)}

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
        try:
          with zipfile.ZipFile(tmp_output, 'w', zipfile.ZIP_DEFLATED) as zout:
            # EPUB OCF 規範：mimetype 必須是第一個 entry 且不壓縮
            all_items = zf.infolist()
            ordered_items = ([i for i in all_items if i.filename == 'mimetype'] +
                             [i for i in all_items if i.filename != 'mimetype'])
            for item in ordered_items:
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
                        data = convert_encoded_refs(converter.convert(text), converter).encode('utf-8')
                    except UnicodeDecodeError:
                        pass  # 解不了就當二進位檔原樣保留，不要讓整批失敗

                zi = zipfile.ZipInfo(new_name, date_time=item.date_time)
                zi.external_attr = item.external_attr
                if new_name == 'mimetype':
                    zi.compress_type = zipfile.ZIP_STORED
                else:
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.flag_bits |= 0x800  # 標記檔名為 UTF-8，避免中文檔名在部分閱讀器顯示異常
                zout.writestr(zi, data)
        except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError) as ex:
            # 讀來源 entry 時失敗（例如標準模式下 CRC 不符）= 來源問題，不是轉換造成
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            raise EpubSourceError(f'讀取來源檔案內容失敗: {ex}')
        except BaseException:
            if os.path.exists(tmp_output):
                os.remove(tmp_output)
            raise

    # 轉換完成，再驗證一次，並跟轉換前的問題清單比對。
    # 只有「轉換後才出現、轉換前沒有」的問題，才算是轉換程式的責任。
    # 本來就存在的瑕疵（製作工具留下的佔位參照之類）照樣放行，不能怪轉換程式。
    try:
        with zipfile.ZipFile(tmp_output, 'r') as zf2:
            post_fatal, post_nonfatal = check_integrity(zf2)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, KeyError) as ex:
        # 輸出檔本身打不開 = 我們寫壞了，這才是轉換程式的責任
        if os.path.exists(tmp_output):
            os.remove(tmp_output)
        return {'status': 'error',
                'reason': '轉換後的輸出檔案無法開啟（轉換程式造成的損壞），已中止並移除輸出檔案',
                'problems': [str(ex)]}

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
    if repaired:
        # 來源 zip 目錄索引損毀（內容完整、CRC 全數核對通過），已在重寫時修好；
        # 由呼叫端（run.ps1）決定怎麼顯示，這裡只回報事實
        result['repaired'] = True
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
        repaired = False
        details = []
        try:
            with open_epub(input_path) as zf:
                repaired = bool(getattr(zf, 'repaired', False))
                fatal, nonfatal = check_integrity(zf)
                if fatal:
                    details = describe_source_problems(zf, fatal, nonfatal)
        except (EpubSourceError, zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, KeyError) as ex:
            fatal, nonfatal = [str(ex) or '不是合法的 zip / epub 檔案（可能已損毀）'], []
            details = describe_open_error(str(ex))
        if fatal:
            result = {'status': 'source_broken', 'reason': '來源檔案本身結構損壞',
                      'problems': fatal, 'nonfatal': nonfatal, 'details': details}
        else:
            result = {'status': 'ok', 'nonfatal': nonfatal}
            if repaired:
                result['repaired'] = True

    elif mode == 'convert':
        input_path = sys.argv[2]
        output_path = sys.argv[3]
        cc_config = 's2twp'
        if '-c' in sys.argv:
            cc_config = sys.argv[sys.argv.index('-c') + 1]
        try:
            result = convert_epub(input_path, output_path, cc_config)
        except (EpubSourceError, zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, KeyError) as ex:
            # 來源讀不了（含寬鬆模式也救不回來）= 來源損壞，不是轉換造成的
            result = {'status': 'source_broken',
                      'reason': '來源檔案本身結構損壞（轉換前即無法讀取）',
                      'problems': [str(ex) or '不是合法的 zip / epub 檔案（可能已損毀）'],
                      'details': describe_open_error(str(ex))}

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
