"""사용설명서.md -> 창의진교회_설교영상편집기_사용설명서.docx

pandoc 으로 만들면 표지가 사라지고 화면 그림(┌─┐)이 고정폭 글꼴을 잃어
줄이 어긋난다. 그래서 docx 를 직접 만든다. 필요한 것은 파이썬뿐이다.

    python tools/make_manual.py

원본 서식은 2026-08 판(버전 1.4) 설명서에서 그대로 가져왔다.
  본문 맑은 고딕 10pt · 화면 그림 Consolas 8pt · 제목 남색 1F4E79
"""
from __future__ import annotations

import datetime as _dt
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
DOCS = HERE.parent / "docs"
SRC = DOCS / "사용설명서.md"
OUT = DOCS / "창의진교회_설교영상편집기_사용설명서.docx"

VERSION = "버전 1.5"
ISSUED = "2026년 9월"

COVER = [
    ("창의진교회", dict(sz=28, color="6B7280", before=1400)),
    ("설교영상 편집기", dict(sz=60, color="1F4E79", bold=True, before=120)),
    ("사용 설명서", dict(sz=32, color="374151", before=260)),
    ("예배 영상에서 설교와 축도만 뽑아", dict(sz=22, color="4B5563", before=900)),
    ("하나의 영상으로 만들어 주는 프로그램", dict(sz=22, color="4B5563", before=60)),
    ("핵심만 골라 3분 요약도 만들어 줍니다", dict(sz=22, color="4B5563", before=60)),
    (f"{VERSION}  ·  {ISSUED}", dict(sz=20, color="9CA3AF", before=1200)),
]

FONT = "맑은 고딕"
MONO = "Consolas"

# ─────────────────────────────────────────────────────────── 조각 만들기


def _fonts(name: str) -> str:
    return (f'<w:rFonts w:ascii="{name}" w:cs="{name}" '
            f'w:eastAsia="{name}" w:hAnsi="{name}"/>')


def run(text: str, *, font: str = FONT, sz: int = 20, bold: bool = False,
        color: str = "", shade: str = "") -> str:
    rpr = [_fonts(font)]
    if bold:
        rpr.append("<w:b/><w:bCs/>")
    if color:
        rpr.append(f'<w:color w:val="{color}"/>')
    rpr.append(f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>')
    if shade:
        rpr.append(f'<w:shd w:fill="{shade}" w:val="clear"/>')
    return (f"<w:r><w:rPr>{''.join(rpr)}</w:rPr>"
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>')


def para(runs: str, *, style: str = "", jc: str = "", before: int = 0,
         after: int = 80, line: int = 290, ind: int = 0, numid: int = 0,
         ilvl: int = 0, shade: str = "", left_bar: str = "",
         keep_next: bool = False, exact: bool = False) -> str:
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if numid:
        ppr.append(f'<w:numPr><w:ilvl w:val="{ilvl}"/>'
                   f'<w:numId w:val="{numid}"/></w:numPr>')
    if left_bar:
        ppr.append(f'<w:pBdr><w:left w:val="single" w:color="{left_bar}" '
                   f'w:sz="12" w:space="6"/></w:pBdr>')
    if shade:
        ppr.append(f'<w:shd w:fill="{shade}" w:val="clear"/>')
    if keep_next:
        ppr.append("<w:keepNext/>")
    sp = f'<w:spacing w:after="{after}"'
    if before:
        sp += f' w:before="{before}"'
    if line:
        rule = "exact" if exact else "auto"
        sp += f' w:line="{line}" w:lineRule="{rule}"'
    ppr.append(sp + "/>")
    if jc:
        ppr.append(f'<w:jc w:val="{jc}"/>')
    if ind:
        ppr.append(f'<w:ind w:left="{ind}"/>')
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{runs}</w:p>"


_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


def inline(text: str, *, sz: int = 20, bold_all: bool = False) -> str:
    """**굵게** 와 `고정폭` 을 살려 run 들로 바꾼다."""
    out, at = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > at:
            out.append(run(text[at:m.start()], sz=sz, bold=bold_all))
        if m.group(1) is not None:
            out.append(inline(m.group(1), sz=sz, bold_all=True))
        else:
            out.append(run(m.group(2), font=MONO, sz=sz - 1,
                           bold=bold_all, shade="F4F5F7"))
        at = m.end()
    if at < len(text):
        out.append(run(text[at:], sz=sz, bold=bold_all))
    return "".join(out) or run("", sz=sz)


# Consolas 에 없는 글자 -> 같은 모양으로 있는 글자 (Word 글꼴 대체 방지)
MONO_SAFE = {"\u2611": "\u25a0", "\u2610": "\u25a1"}


def code_line(text: str) -> str:
    for bad, good in MONO_SAFE.items():
        text = text.replace(bad, good)
    return para(run(text, font=MONO, sz=16), after=0, line=200,
                exact=True, ind=120,
                shade="F4F5F7", left_bar="C9CDD4")


def cell(runs: str, width: int, *, head: bool = False) -> str:
    shd = '<w:shd w:fill="E8EEF6" w:val="clear"/>' if head else ""
    return (f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{width}"/>{shd}'
            '<w:tcMar><w:top w:type="dxa" w:w="60"/>'
            '<w:left w:type="dxa" w:w="110"/>'
            '<w:bottom w:type="dxa" w:w="60"/>'
            '<w:right w:type="dxa" w:w="110"/></w:tcMar></w:tcPr>'
            f'{para(runs, after=0, line=260)}</w:tc>')


def table(rows: list[list[str]]) -> str:
    ncol = max(len(r) for r in rows)
    total = 9360
    widths = [total // ncol] * ncol
    widths[0] = total - sum(widths[1:])
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    out = ['<w:tbl><w:tblPr><w:tblW w:type="dxa" w:w="9360"/><w:tblBorders>'
           + "".join(f'<w:{e} w:val="single" w:color="auto" w:sz="4"/>'
                     for e in ("top", "left", "bottom", "right",
                               "insideH", "insideV"))
           + f"</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>"]
    for i, r in enumerate(rows):
        head = i == 0
        tr = "<w:trPr><w:tblHeader/></w:trPr>" if head else ""
        cells = "".join(
            cell(inline(r[c] if c < len(r) else "", sz=19, bold_all=head),
                 widths[c], head=head)
            for c in range(ncol))
        out.append(f"<w:tr>{tr}{cells}</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


# ─────────────────────────────────────────────────────────── 마크다운 읽기

def parse(md: str) -> list[tuple]:
    """(종류, 값) 목록으로 바꾼다. 종류: h1 h2 h3 p ul ol code table quote"""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    lines = md.split("\n")
    items: list[tuple] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            body, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            items.append(("code", body))
            continue
        if s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                raw = lines[i].strip().strip("|")
                if not re.fullmatch(r"[\s:|-]+", raw):
                    rows.append([c.strip() for c in raw.split("|")])
                i += 1
            items.append(("table", rows))
            continue
        if re.fullmatch(r"-{3,}", s):
            i += 1
            continue
        if s.startswith("#"):
            lvl = len(s) - len(s.lstrip("#"))
            items.append((f"h{min(lvl, 3)}", s.lstrip("# ").strip()))
            i += 1
            continue
        if s.startswith("> "):
            body = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                body.append(lines[i].strip().lstrip(">").strip())
                i += 1
            items.append(("quote", " ".join(body)))
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            kind = "ul" if m.group(2) in "-*" else "ol"
            entries: list[tuple[int, str]] = []
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if mm:
                    depth = 1 if len(mm.group(1)) >= 2 else 0
                    sub = "ul" if mm.group(2) in "-*" else "ol"
                    entries.append((depth, mm.group(3).strip(), sub))
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")):
                    d, t, sub = entries[-1]
                    entries[-1] = (d, t + " " + lines[i].strip(), sub)
                    i += 1
                else:
                    break
            items.append((kind, entries))
            continue
        body = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^\s*([-*]|\d+\.|>|#|\||```|-{3,})", lines[i]):
            body.append(lines[i].strip())
            i += 1
        items.append(("p", " ".join(body)))
    return items


# ─────────────────────────────────────────────────────────── 문서 짜기

def build_body(items: list[tuple]) -> tuple[str, int]:
    out: list[str] = []
    numid = 1              # 1 = 글머리표, 2 부터 번호 목록(하나씩 새로 시작)
    used_ol = 0
    for kind, val in items:
        if kind == "h1":
            continue        # 제목은 표지에 넣었다
        if kind == "h2":
            txt = re.sub(r"^(\d+)\.\s*", r"\1.  ", val)
            out.append(para(run(txt, sz=30, bold=True, color="1F4E79"),
                            style="Heading1", before=360, after=160,
                            keep_next=True))
        elif kind == "h3":
            out.append(para(run(val, sz=24, bold=True, color="2E5F8A"),
                            style="Heading2", before=260, after=110,
                            keep_next=True))
        elif kind == "p":
            out.append(para(inline(val)))
        elif kind == "quote":
            out.append(para(inline(val, sz=19), shade="F7F8FA",
                            left_bar="1F4E79", ind=120,
                            before=60, after=120))
        elif kind == "code":
            for line in val:
                out.append(code_line(line))
            out.append(para("", after=100, line=120))
        elif kind == "table":
            out.append(table(val))
            out.append(para("", after=60, line=120))
        elif kind in ("ul", "ol"):
            if kind == "ol":
                used_ol += 1
                base = 1 + used_ol
            else:
                base = 1
            for depth, text, sub in val:
                nid = 1 if (depth or sub == "ul") else base
                out.append(para(inline(text), style="ListParagraph",
                                numid=nid, ilvl=depth, after=70))
            out.append(para("", after=40, line=120))
    return "".join(out), used_ol


def cover() -> str:
    out = []
    for text, kw in COVER:
        out.append(para(run(text, sz=kw["sz"], color=kw["color"],
                            bold=kw.get("bold", False)),
                        jc="center", before=kw["before"], after=0, line=0))
    out.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    return "".join(out)


def contents(items: list[tuple]) -> str:
    out = [para(run("차례", sz=30, bold=True, color="1F4E79"),
                before=120, after=200, line=340, exact=True)]
    for kind, val in items:
        if kind == "h2":
            txt = re.sub(r"^(\d+)\.\s*", r"\1.  ", val)
            out.append(para(run(txt, sz=21, bold=True, color="374151"),
                            after=0, before=100, line=280, exact=True))
        elif kind == "h3":
            out.append(para(run(val, sz=18, color="4B5563"),
                            ind=520, after=0, line=240, exact=True))
    out.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    return "".join(out)


NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
      'relationships"')

SECTPR = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838" w:orient="portrait"/>'
          '<w:pgMar w:top="1200" w:right="1080" w:bottom="1200" w:left="1080" '
          'w:header="708" w:footer="708" w:gutter="0"/>'
          '<w:docGrid w:linePitch="360"/></w:sectPr>')

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {NS}><w:docDefaults><w:rPrDefault><w:rPr>{_fonts(FONT)}
<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="290"
w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:color w:val="1F4E79"/><w:sz w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>
<w:pPr><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:color w:val="2E5F8A"/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph">
<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/>
</w:style></w:styles>"""


def numbering(count: int) -> str:
    abst, nums = [], []
    bullet = ('<w:lvl w:ilvl="0"><w:start w:val="1"/>'
              '<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
              '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="460" '
              'w:hanging="240"/></w:pPr></w:lvl>'
              '<w:lvl w:ilvl="1"><w:start w:val="1"/>'
              '<w:numFmt w:val="bullet"/><w:lvlText w:val="–"/>'
              '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="880" '
              'w:hanging="240"/></w:pPr></w:lvl>')
    dec = ('<w:lvl w:ilvl="0"><w:start w:val="1"/>'
           '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
           '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="460" '
           'w:hanging="300"/></w:pPr></w:lvl>'
           '<w:lvl w:ilvl="1"><w:start w:val="1"/>'
           '<w:numFmt w:val="bullet"/><w:lvlText w:val="–"/>'
           '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="880" '
           'w:hanging="240"/></w:pPr></w:lvl>')
    abst.append(f'<w:abstractNum w:abstractNumId="1">{bullet}</w:abstractNum>')
    nums.append('<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>')
    for k in range(count):
        aid = 2 + k
        abst.append(f'<w:abstractNum w:abstractNumId="{aid}">{dec}'
                    "</w:abstractNum>")
        nums.append(f'<w:num w:numId="{aid}">'
                    f'<w:abstractNumId w:val="{aid}"/></w:num>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:numbering {NS}>{''.join(abst)}{''.join(nums)}</w:numbering>")


CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DRELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
</Relationships>"""

SETTINGS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:settings {NS}><w:compat/></w:settings>')


def props() -> tuple[str, str]:
    now = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
            'package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:title>창의진교회 설교영상 편집기 — 사용 설명서</dc:title>"
            f"<dc:description>{VERSION} · {ISSUED}</dc:description>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}'
            "</dcterms:created>"
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}'
            "</dcterms:modified></cp:coreProperties>")
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<Properties xmlns="http://schemas.openxmlformats.org/'
           'officeDocument/2006/extended-properties">'
           "<Application>sermon-tool/tools/make_manual.py</Application>"
           "</Properties>")
    return core, app


def main() -> None:
    items = parse(SRC.read_text(encoding="utf-8"))
    body, n_ol = build_body(items)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f"<w:document {NS}><w:body>"
           f"{cover()}{contents(items)}{body}{SECTPR}</w:body></w:document>")
    core, app = props()
    tmp = OUT.with_suffix(".docx.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", DRELS)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", numbering(n_ol))
        z.writestr("word/settings.xml", SETTINGS)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
    tmp.replace(OUT)
    n = {k: sum(1 for a, _ in items if a == k) for k in
         ("h2", "h3", "p", "code", "table", "ul", "ol", "quote")}
    print(f"만들었습니다: {OUT.name}  ({OUT.stat().st_size:,} 바이트)")
    print("  장 %(h2)d · 절 %(h3)d · 문단 %(p)d · 화면그림 %(code)d "
          "· 표 %(table)d · 목록 %(ul)d+%(ol)d · 강조상자 %(quote)d" % n)


if __name__ == "__main__":
    main()
