"""Fill the official CCB BASIC self-assessment workbook with the scores from this tool.

The CCB workbook is a protected Excel file that must be handed to the CCB or the
Conformity Assessment Body as-is. It is not redistributed with this project; the
user uploads their own copy and receives it back filled in.

To keep the file identical to the original apart from the entered values, the
workbook is edited at the XML level inside the .xlsx package:

* Documentation and implementation scores go into columns F and G of the function
  sheets (numeric 1..5, or the text "N/A").
* Comments go into column L ("Comments and/or additional information").
* The completion date goes into Introduction!T27 as an Excel date serial.
* Cached values of formula cells are recomputed with a small evaluator so that
  readers that do not recalculate (scripts, previews) see correct numbers, and
  fullCalcOnLoad is set so Excel recalculates everything on open anyway.

Nothing else is touched: sheet protection, data validation, conditional formatting,
the summary chart, styles, shared strings and custom XML parts are copied verbatim.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import escape

from .framework import Framework

DEFAULT_LAYOUT = {
    "level_col": None,
    "req_col": "E",
    "doc_col": "F",
    "impl_col": "G",
    "comment_col": "L",
    "date_cell": "T27",
    "intro_sheet": "Introduction",
    "summary_sheet": "BASIC Summary",
    "function_sheets": ["GOVERN", "IDENTIFY", "PROTECT", "DETECT", "RESPOND", "RECOVER"],
}

_CELL_RE_TMPL = r'<c r="{ref}"((?:\s[^>]*?)?)(?:/>|>(.*?)</c>)'


class ExportError(ValueError):
    pass


class _DeferredError(Exception):
    """A formula references a formula cell that has not been computed yet."""


@dataclass
class ExportInput:
    doc: int | None
    impl: int | None
    not_applicable: bool
    comment: str


# --------------------------------------------------------------------------- helpers
def col_to_num(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def num_to_col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"\$?([A-Z]{1,3})\$?(\d+)", ref)
    if not m:
        raise ValueError(ref)
    return m.group(1), int(m.group(2))


def excel_serial(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def _read_shared_strings(xml: str) -> list[str]:
    out = []
    for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
        out.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
    return out


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


# --------------------------------------------------------------------------- package
class Package:
    def __init__(self, data: bytes):
        self.zin = zipfile.ZipFile(io.BytesIO(data))
        self.parts: dict[str, bytes] = {n: self.zin.read(n) for n in self.zin.namelist()}
        self.order = self.zin.namelist()
        wb = self.parts["xl/workbook.xml"].decode("utf-8")
        rels = self.parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
        rid_target: dict[str, str] = {}
        for rel in re.findall(r"<Relationship [^>]+>", rels):
            rid = re.search(r'Id="([^"]+)"', rel)
            tgt = re.search(r'Target="([^"]+)"', rel)
            if rid and tgt:
                rid_target[rid.group(1)] = tgt.group(1)
        self.sheet_part: dict[str, str] = {}
        for m in re.finditer(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb):
            target = rid_target.get(m.group(2), "")
            self.sheet_part[_unescape(m.group(1))] = "xl/" + target.lstrip("/").removeprefix("xl/")
        self.shared = _read_shared_strings(self.parts.get("xl/sharedStrings.xml", b"").decode("utf-8"))

    def sheet_xml(self, name: str) -> str:
        part = self.sheet_part.get(name)
        if not part or part not in self.parts:
            raise ExportError(f"Sheet '{name}' not found in the workbook.")
        return self.parts[part].decode("utf-8")

    def set_sheet_xml(self, name: str, xml: str) -> None:
        self.parts[self.sheet_part[name]] = xml.encode("utf-8")

    def cell(self, xml: str, ref: str) -> re.Match | None:
        return re.search(_CELL_RE_TMPL.format(ref=ref), xml, re.S)

    def cell_text(self, name: str, ref: str) -> str:
        """Literal text of a cell (shared or inline string, or number as text)."""
        m = self.cell(self.sheet_xml(name), ref)
        if not m:
            return ""
        attrs, inner = m.group(1) or "", m.group(2) or ""
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        if 't="s"' in attrs and v:
            return self.shared[int(v.group(1))]
        if 't="inlineStr"' in attrs:
            return "".join(re.findall(r"<t[^>]*>(.*?)</t>", inner, re.S))
        return _unescape(v.group(1)) if v else ""

    def write(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in self.order:
                info = self.zin.getinfo(name)
                zi = zipfile.ZipInfo(name, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zout.writestr(zi, self.parts[name])
        return buf.getvalue()


def _set_cell(xml: str, ref: str, value: float | int | str | None, keep_style: bool = True) -> str:
    """Replace the content of a value cell. Numbers become <v>, strings become inline strings."""
    m = re.search(_CELL_RE_TMPL.format(ref=ref), xml, re.S)
    attrs = (m.group(1) or "") if m else ""
    style = re.search(r'\ss="(\d+)"', attrs)
    s_attr = f' s="{style.group(1)}"' if (style and keep_style) else ""
    if value is None or value == "":
        new = f'<c r="{ref}"{s_attr}/>'
    elif isinstance(value, str):
        new = f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'
    else:
        num = int(value) if float(value).is_integer() else value
        new = f'<c r="{ref}"{s_attr}><v>{num}</v></c>'
    if m:
        return xml[: m.start()] + new + xml[m.end() :]
    return _insert_cell(xml, ref, new)


def _insert_cell(xml: str, ref: str, cell_xml: str) -> str:
    """Insert a cell element into its row, keeping cells in column order. The row must exist."""
    col, row = split_ref(ref)
    rm = re.search(rf'<row r="{row}"(?:\s[^>]*?)?(?:/>|>(.*?)</row>)', xml, re.S)
    if not rm:
        raise ExportError(f"Row {row} not found in sheet; the workbook layout differs from the supported version.")
    if rm.group(1) is None:  # self-closing empty row
        head = rm.group(0)[:-2] + ">"
        return xml[: rm.start()] + head + cell_xml + "</row>" + xml[rm.end() :]
    inner = rm.group(1)
    target = col_to_num(col)
    insert_at = len(inner)
    for cm in re.finditer(r'<c r="([A-Z]+)\d+"', inner):
        if col_to_num(cm.group(1)) > target:
            insert_at = cm.start()
            break
    new_inner = inner[:insert_at] + cell_xml + inner[insert_at:]
    row_start = rm.start() + rm.group(0).index(">") + 1
    return xml[:row_start] + new_inner + "</row>" + xml[rm.end() :]


def _set_cached(xml: str, ref: str, value: float) -> str:
    """Update the cached <v> of a formula cell, leaving the formula untouched."""
    m = re.search(_CELL_RE_TMPL.format(ref=ref), xml, re.S)
    if not m or m.group(2) is None or "<f" not in m.group(2):
        return xml
    inner = m.group(2)
    num = int(value) if float(value).is_integer() else round(value, 10)
    inner2 = re.sub(r"<v\s*/>|<v>.*?</v>", "", inner, flags=re.S) + f"<v>{num}</v>"
    attrs = (m.group(1) or "").replace(' t="str"', "").replace(' t="e"', "")
    new = f'<c r="{ref}"{attrs}>{inner2}</c>'
    return xml[: m.start()] + new + xml[m.end() :]


# --------------------------------------------------------------------------- formulas
class _Evaluator:
    """Evaluates the formula subset used by the CCB workbook (refs, ranges, AVERAGE, SUM,
    COUNT, MIN, MAX, IF, OR, AND, arithmetic, string equality)."""

    TOKEN = re.compile(
        r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>\"[^\"]*\")|(?P<ref>(?:'[^']+'|[A-Za-z_][\w. ]*)!\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?|\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)|(?P<name>[A-Za-z_][A-Za-z_0-9.]*)|(?P<op>[-+*/(),=<>]))"
    )

    def __init__(self, values: dict[str, dict[str, object]], sheet: str, pending: dict[str, dict[str, str]] | None = None):
        self.values = values
        self.sheet = sheet
        self.pending = pending or {}

    def _lookup(self, sheet: str, ref: str) -> object:
        col, row = split_ref(ref)
        key = f"{col}{row}"
        if key in self.pending.get(sheet, {}):
            raise _DeferredError(f"{sheet}!{key}")
        return self.values.get(sheet, {}).get(key)

    def _expand(self, token: str) -> list[object]:
        sheet = self.sheet
        if "!" in token:
            sh, token = token.rsplit("!", 1)
            sheet = sh.strip("'")
        if ":" in token:
            a, b = token.split(":")
            c1, r1 = split_ref(a)
            c2, r2 = split_ref(b)
            out = []
            for r in range(r1, r2 + 1):
                for c in range(col_to_num(c1), col_to_num(c2) + 1):
                    out.append(self._lookup(sheet, f"{num_to_col(c)}{r}"))
            return out
        return [self._lookup(sheet, token)]

    def evaluate(self, formula: str) -> object:
        self.tokens = []
        pos = 0
        while pos < len(formula):
            m = self.TOKEN.match(formula, pos)
            if not m or m.end() == pos:
                raise ValueError(f"cannot tokenise: {formula!r} at {pos}")
            pos = m.end()
            self.tokens.append(m)
        self.i = 0
        val = self._comparison()
        if self.i != len(self.tokens):
            raise ValueError(f"trailing tokens in {formula!r}")
        return val

    def _peek(self) -> re.Match | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> re.Match:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def _comparison(self):
        left = self._expr()
        t = self._peek()
        if t and t.group("op") == "=":
            self._take()
            right = self._expr()
            return _norm(left) == _norm(right)
        return left

    def _expr(self):
        val = self._term()
        while True:
            t = self._peek()
            if t and t.group("op") in ("+", "-"):
                self._take()
                rhs = self._term()
                val = _num(val) + _num(rhs) if t.group("op") == "+" else _num(val) - _num(rhs)
            else:
                return val

    def _term(self):
        val = self._factor()
        while True:
            t = self._peek()
            if t and t.group("op") in ("*", "/"):
                self._take()
                rhs = self._factor()
                val = _num(val) * _num(rhs) if t.group("op") == "*" else _num(val) / _num(rhs)
            else:
                return val

    def _factor(self):
        t = self._take()
        if t.group("num"):
            return float(t.group("num"))
        if t.group("str"):
            return t.group("str")[1:-1]
        if t.group("ref"):
            vals = self._expand(t.group("ref"))
            return vals if ":" in t.group("ref") else vals[0]
        if t.group("op") == "(":
            v = self._comparison()
            self._expect(")")
            return v
        if t.group("op") == "-":
            return -_num(self._factor())
        if t.group("name"):
            name = t.group("name").upper()
            self._expect("(")
            args: list[object] = []
            if not (self._peek() and self._peek().group("op") == ")"):
                args.append(self._comparison())
                while self._peek() and self._peek().group("op") == ",":
                    self._take()
                    args.append(self._comparison())
            self._expect(")")
            return self._call(name, args)
        raise ValueError(f"unexpected token {t.group(0)!r}")

    def _expect(self, op: str) -> None:
        t = self._take()
        if t.group("op") != op:
            raise ValueError(f"expected {op!r}")

    @staticmethod
    def _flat(args: list[object]) -> list[object]:
        out: list[object] = []
        for a in args:
            if isinstance(a, list):
                out.extend(a)
            else:
                out.append(a)
        return out

    def _call(self, name: str, args: list[object]):
        if name == "IF":
            cond = args[0]
            return args[1] if bool(cond) else (args[2] if len(args) > 2 else False)
        if name == "OR":
            return any(bool(a) for a in self._flat(args))
        if name == "AND":
            return all(bool(a) for a in self._flat(args))
        nums = [float(v) for v in self._flat(args) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if name == "SUM":
            return sum(nums)
        if name == "COUNT":
            return float(len(nums))
        if name == "AVERAGE":
            if not nums:
                raise ZeroDivisionError("AVERAGE of no numbers")
            return sum(nums) / len(nums)
        if name == "MIN":
            return min(nums)
        if name == "MAX":
            return max(nums)
        raise ValueError(f"unsupported function {name}")


def _norm(v: object) -> object:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return ""
    return str(v)


def _num(v: object) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if v is None or v == "":
        return 0.0
    raise ValueError(f"not a number: {v!r}")


def _shift_formula(formula: str, d_col: int, d_row: int) -> str:
    def repl(m: re.Match) -> str:
        c_abs, col, r_abs, row = m.group(1), m.group(2), m.group(3), int(m.group(4))
        if not c_abs:
            col = num_to_col(col_to_num(col) + d_col)
        if not r_abs:
            row += d_row
        return f"{c_abs}{col}{r_abs}{row}"

    parts = re.split(r'("[^"]*")', formula)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"(\$?)([A-Z]{1,3})(\$?)(\d+)", repl, parts[i])
    return "".join(parts)


def _sheet_cells(xml: str) -> list[tuple[str, str, str | None]]:
    """(ref, attrs, inner) for every cell element in the sheet."""
    out = []
    for m in re.finditer(r'<c r="([A-Z]+\d+)"((?:\s[^>]*?)?)(?:/>|>(.*?)</c>)', xml, re.S):
        out.append((m.group(1), m.group(2) or "", m.group(3)))
    return out


def _collect_values_and_formulas(pkg: Package, sheets: list[str]):
    values: dict[str, dict[str, object]] = {}
    formulas: dict[str, dict[str, str]] = {}
    for name in sheets:
        xml = pkg.sheet_xml(name)
        values[name] = {}
        formulas[name] = {}
        shared_master: dict[str, tuple[str, str]] = {}  # si -> (ref, formula)
        for ref, attrs, inner in _sheet_cells(xml):
            if inner is None:
                continue
            f = re.search(r"<f([^>]*)>(.*?)</f>|<f([^>]*)/>", inner, re.S)
            v = re.search(r"<v>(.*?)</v>", inner, re.S)
            if f:
                fattrs = f.group(1) if f.group(1) is not None else (f.group(3) or "")
                text = _unescape(f.group(2) or "")
                si = re.search(r'si="(\d+)"', fattrs)
                if 't="shared"' in fattrs and si:
                    if text:
                        shared_master[si.group(1)] = (ref, text)
                    else:
                        mref, mtext = shared_master[si.group(1)]
                        mc, mr = split_ref(mref)
                        c, r = split_ref(ref)
                        text = _shift_formula(mtext, col_to_num(c) - col_to_num(mc), r - mr)
                if text:
                    formulas[name][ref] = text
                continue
            if v:
                raw = _unescape(v.group(1))
                if 't="s"' in attrs:
                    values[name][ref] = pkg.shared[int(raw)]
                elif 't="str"' in attrs or 't="inlineStr"' in attrs:
                    values[name][ref] = raw
                elif 't="b"' in attrs:
                    values[name][ref] = raw == "1"
                else:
                    try:
                        values[name][ref] = float(raw)
                    except ValueError:
                        values[name][ref] = raw
            elif 't="inlineStr"' in attrs:
                values[name][ref] = "".join(re.findall(r"<t[^>]*>(.*?)</t>", inner, re.S))
    return values, formulas


def _recalculate(pkg: Package, sheets: list[str]) -> int:
    """Evaluate formula cells in the given sheets and refresh their cached values."""
    values, formulas = _collect_values_and_formulas(pkg, sheets)
    computed: dict[str, dict[str, float]] = {s: {} for s in sheets}
    pending = {s: dict(fs) for s, fs in formulas.items()}
    unsupported: dict[str, set[str]] = {s: set() for s in sheets}
    for _ in range(20):
        progress = False
        for sheet in sheets:
            ev = _Evaluator(values, sheet, pending)
            for ref, formula in list(pending[sheet].items()):
                try:
                    result = ev.evaluate(formula)
                except _DeferredError:
                    continue
                except Exception:
                    # Unsupported formula: leave its cached value alone; Excel recalculates on load.
                    pending[sheet].pop(ref)
                    unsupported[sheet].add(ref)
                    progress = True
                    continue
                pending[sheet].pop(ref)
                progress = True
                if isinstance(result, bool) or not isinstance(result, (int, float)):
                    continue
                values[sheet][ref] = float(result)
                computed[sheet][ref] = float(result)
        if not progress or not any(pending.values()):
            break
    count = 0
    for sheet in sheets:
        xml = pkg.sheet_xml(sheet)
        for ref, val in computed[sheet].items():
            xml = _set_cached(xml, ref, val)
            count += 1
        pkg.set_sheet_xml(sheet, xml)
    return count


# --------------------------------------------------------------------------- public API
def layout_of(fw: Framework) -> dict:
    return {**DEFAULT_LAYOUT, **(getattr(fw, "layout", None) or {})}


def validate_template(pkg: Package, fw: Framework) -> list[str]:
    lay = layout_of(fw)
    problems: list[str] = []
    for name in (lay["intro_sheet"], lay["summary_sheet"], *lay["function_sheets"]):
        if name not in pkg.sheet_part:
            problems.append(f"Sheet '{name}' is missing (expected the CCB {fw.level} tool, version {fw.tool_version}).")
    if problems:
        return problems
    col = lay["req_col"]
    for req in fw.requirements:
        text = pkg.cell_text(req.sheet, f"{col}{req.row}")
        if not text.strip().startswith(req.workbook_id):
            problems.append(f"{req.sheet}!{col}{req.row} should hold {req.workbook_id}; found: {text[:60]!r}")
    return problems


def fill_workbook(
    template: bytes,
    fw: Framework,
    inputs: dict[str, ExportInput],
    completion_date: date | None,
) -> tuple[bytes, dict]:
    try:
        pkg = Package(template)
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError) as exc:
        raise ExportError("The uploaded file is not an Excel workbook (.xlsx).") from exc
    problems = validate_template(pkg, fw)
    if problems:
        raise ExportError(f"The uploaded file is not the supported CCB {fw.level} workbook: " + " ".join(problems[:3]))
    lay = layout_of(fw)
    doc_col, impl_col, comment_col = lay["doc_col"], lay["impl_col"], lay["comment_col"]

    written = 0
    for sheet in lay["function_sheets"]:
        xml = pkg.sheet_xml(sheet)
        for req in [r for r in fw.requirements if r.sheet == sheet]:
            inp = inputs.get(req.id)
            if inp is None:
                continue
            if inp.not_applicable:
                xml = _set_cell(xml, f"{doc_col}{req.row}", "N/A")
                xml = _set_cell(xml, f"{impl_col}{req.row}", "N/A")
            else:
                xml = _set_cell(xml, f"{doc_col}{req.row}", inp.doc)
                xml = _set_cell(xml, f"{impl_col}{req.row}", inp.impl)
            xml = _set_cell(xml, f"{comment_col}{req.row}", inp.comment.strip() or None)
            written += 1
        pkg.set_sheet_xml(sheet, xml)

    if completion_date:
        intro = pkg.sheet_xml(lay["intro_sheet"])
        intro = _set_cell(intro, lay["date_cell"], excel_serial(completion_date))
        pkg.set_sheet_xml(lay["intro_sheet"], intro)

    recalculated = _recalculate(pkg, [lay["intro_sheet"], *lay["function_sheets"], lay["summary_sheet"]])

    wb = pkg.parts["xl/workbook.xml"].decode("utf-8")
    if "fullCalcOnLoad" not in wb:
        wb = re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', wb, count=1)
    pkg.parts["xl/workbook.xml"] = wb.encode("utf-8")

    return pkg.write(), {"requirements_written": written, "formulas_recalculated": recalculated}
