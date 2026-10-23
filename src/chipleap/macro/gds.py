"""Just enough GDSII to check a macro: cell names, top cells, bounding boxes, labels.

The reader walks the record stream once and keeps each cell's shape points, the box
of its own shapes, its references and its labels. It then walks the hierarchy once
without recursion: a cell that contains itself, directly or through other cells, is
an error, and so are references nested deeper than the limit, so everything after it
can recurse safely. A cell's box through its hierarchy is computed once per cell, and
an array reference's box comes from its four corner instances, never from every
instance. Text does not count toward a box, as in KLayout. It has no dependencies, so
ChipLeap and aispice (``aispice_core::macropkg::gds``) read GDS the same way.
"""

from __future__ import annotations

import hashlib
import math
import struct
from array import array
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path

from chipleap.macro.limits import LIMITS, LimitError, Limits, read_capped
from chipleap.macro.text import quote

Box = tuple[float, float, float, float]
Point = tuple[float, float]


class GdsError(ValueError):
    pass


class GdsCycleError(GdsError):
    """A cell contains itself, directly or through other cells."""


@dataclass(slots=True)
class _Ref:
    cell: str
    x: float
    y: float
    mirror: bool = False
    mag: float = 1.0
    angle: float = 0.0
    # Array references: columns, rows and the step between them.
    cols: int = 1
    rows: int = 1
    col_step: tuple[float, float] = (0.0, 0.0)
    row_step: tuple[float, float] = (0.0, 0.0)


@dataclass(slots=True)
class _Cell:
    name: str
    # Every shape's corner points, x then y: a polygon's vertices, a path's box corners.
    points: array[float] = field(default_factory=lambda: array("d"))
    box: Box | None = None  # of the cell's own shapes
    refs: list[_Ref] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


@dataclass
class GdsLibrary:
    """A parsed GDS file. Coordinates are in micrometres."""

    um_per_dbu: float
    cells: dict[str, _Cell]
    limits: Limits = LIMITS
    _boxes: dict[str, Box | None] = field(default_factory=dict, repr=False)
    _hulls: dict[str, list[Point]] = field(default_factory=dict, repr=False)
    _points: int = 0

    def top_cells(self) -> list[str]:
        """Cells no other cell references, in file order."""
        used = {r.cell for c in self.cells.values() for r in c.refs}
        return [n for n in self.cells if n not in used]

    def labels(self, cell: str) -> list[str]:
        """Text strings placed directly in a cell."""
        return list(self.cells[cell].labels)

    def children(self, cell: str) -> list[str]:
        """The cells a cell places, each once, in the order of its references."""
        return list(dict.fromkeys(r.cell for r in self.cells[cell].refs))

    def bbox(self, cell: str) -> Box | None:
        """The cell's bounding box through its hierarchy, or None when it is empty.

        Raises :class:`GdsError` for a reference to a cell the file does not define and
        :class:`LimitError` when measuring it takes too much work or overflows. Each
        call measures afresh, with its own budget, as aispice does.
        """
        self._boxes, self._hulls, self._points = {}, {}, 0
        return _scale(self._box(cell), self.um_per_dbu)

    def _cell(self, name: str) -> _Cell:
        cell = self.cells.get(name)
        if cell is None:
            raise GdsError(f"cell {name} is referenced but not defined")
        return cell

    def _spend(self, points: int) -> None:
        self._points += points
        if self._points > self.limits.gds_points:
            raise LimitError(
                "references at angles other than quarter turns need more than "
                f"{self.limits.gds_points} points to measure"
            )

    def _box(self, name: str) -> Box | None:
        if name in self._boxes:
            return self._boxes[name]
        cell = self._cell(name)
        box = cell.box
        for r in cell.refs:
            corners = _corners(r)
            if r.angle % 90 == 0:
                # A quarter turn maps the child's box onto the placed box exactly.
                child = self._box(r.cell)
                if child is not None:
                    for dx, dy in corners:
                        box = _union(box, _transform(child, r, dx, dy))
            else:
                hull = self._hull(r.cell)
                if hull:
                    self._spend(len(hull) * len(corners))
                    for dx, dy in corners:
                        box = _union(box, _points_box(_place(_affine(r, dx, dy), hull)))
        self._boxes[name] = box
        return box

    def _hull(self, name: str) -> list[Point]:
        """The convex hull of a cell through its hierarchy. Any transform maps it onto
        the hull of the placed cell, so its placed points give the placed box exactly."""
        if name in self._hulls:
            return self._hulls[name]
        cell = self._cell(name)
        children = [(r, _corners(r), self._hull(r.cell)) for r in cell.refs]
        self._spend(len(cell.points) // 2 + sum(len(c) * len(h) for _, c, h in children))
        pts = list(zip(cell.points[0::2], cell.points[1::2], strict=True))
        for r, corners, h in children:
            for dx, dy in corners:
                pts += _place(_affine(r, dx, dy), h)
        hull = _convex_hull(pts)
        self._hulls[name] = hull
        return hull


# x' = a x + b y + e, y' = c x + d y + f
Affine = tuple[float, float, float, float, float, float]

_OVERFLOW = "a reference's magnification takes coordinates out of range"
# The largest coordinate a measurement keeps, in database units: far past any layout,
# and small enough that the hull's cross products of differences cannot overflow.
_FAR = 1e150


def _corners(r: _Ref) -> list[Point]:
    """Offsets of the corner instances of a reference; the others lie between them."""
    return [
        (
            r.x + i * r.col_step[0] + j * r.row_step[0],
            r.y + i * r.col_step[1] + j * r.row_step[1],
        )
        for i in sorted({0, r.cols - 1})
        for j in sorted({0, r.rows - 1})
    ]


def _cos_sin(angle: float) -> tuple[float, float]:
    a = angle % 360
    if a in _QUARTER:
        return _QUARTER[int(a)]
    return math.cos(math.radians(a)), math.sin(math.radians(a))


def _affine(r: _Ref, dx: float, dy: float) -> Affine:
    c, s = _cos_sin(r.angle)
    f = -1.0 if r.mirror else 1.0
    return (r.mag * c, -r.mag * s * f, r.mag * s, r.mag * c * f, dx, dy)


def _apply(m: Affine, pt: Point) -> Point:
    x, y = pt
    return (m[0] * x + m[1] * y + m[4], m[2] * x + m[3] * y + m[5])


def _place(m: Affine, pts: list[Point]) -> list[Point]:
    """Points under a transform. A huge magnification, compounded through the
    hierarchy, can take them out of range, and the box would then be meaningless."""
    out = [_apply(m, p) for p in pts]
    if not all(abs(x) <= _FAR and abs(y) <= _FAR for x, y in out):
        raise LimitError(_OVERFLOW)
    return out


def _points_box(pts: list[Point]) -> Box:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _union(a: Box | None, b: Box) -> Box:
    if a is None:
        return b
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _scale(b: Box | None, k: float) -> Box | None:
    return None if b is None else (b[0] * k, b[1] * k, b[2] * k, b[3] * k)


# Exact sine and cosine for quarter turns, so rotated boxes do not pick up rounding.
_QUARTER = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}


def _transform(b: Box, r: _Ref, dx: float, dy: float) -> Box:
    m = _affine(r, dx, dy)
    return _points_box(_place(m, [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]))


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex_hull(pts: list[Point]) -> list[Point]:
    """Andrew's monotone chain. aispice sorts and compares the same way, so both tools
    keep the same points. Every coordinate is within ``_FAR``, so the cross products
    are finite."""
    pts = sorted(pts)
    if len(pts) <= 2:
        return pts
    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _need(body: bytes, n: int, what: str) -> bytes:
    if len(body) < n:
        raise GdsError(f"{what} record is too short")
    return body[:n]


def _real8(b: bytes) -> float:
    sign = -1.0 if b[0] & 0x80 else 1.0
    exp = (b[0] & 0x7F) - 64
    mant = int.from_bytes(b[1:8], "big")
    return sign * mant / float(1 << 56) * 16.0**exp


def _path_box(xy: list[Point], width: float, pathtype: int, ext: tuple[float, float]) -> Box:
    """The box of a path: half the width around every point, and along the path at its
    two ends only as far as the path type extends it (flush ends do not)."""
    w = abs(width) / 2
    begin, end = (w, w) if pathtype in (1, 2) else ext if pathtype == 4 else (0.0, 0.0)
    xs: list[float] = []
    ys: list[float] = []
    n = len(xy)
    for k, (x, y) in enumerate(xy):
        ex = ey = w
        if n > 1 and k in (0, n - 1):
            ox, oy = xy[1] if k == 0 else xy[n - 2]
            along = begin if k == 0 else end
            if oy == y:  # horizontal end segment
                ex = along
            elif ox == x:  # vertical end segment
                ey = along
        xs += [x - ex, x + ex]
        ys += [y - ey, y + ey]
    return (min(xs), min(ys), max(xs), max(ys))


def _check_hierarchy(cells: dict[str, _Cell], limits: Limits) -> None:
    """Depth-first over every cell in file order and every reference in element order,
    with an explicit stack: the first cycle found, or the first cell with references
    nested deeper than the limit below it, is an error."""
    height: dict[str, int] = {}
    for root in cells:
        if root in height:
            continue
        path = [root]
        on_path = {root}
        stack = [(root, 0)]
        while stack:
            name, k = stack[-1]
            refs = cells[name].refs
            if k < len(refs):
                stack[-1] = (name, k + 1)
                child = refs[k].cell
                if child in on_path:
                    cycle = [*path[path.index(child) :], child]
                    raise GdsCycleError(
                        f"cell {quote(child)} contains itself: "
                        + " -> ".join(quote(c) for c in cycle)
                    )
                if child in height or child not in cells:
                    continue
                path.append(child)
                on_path.add(child)
                stack.append((child, 0))
                continue
            h = max((height[r.cell] + 1 for r in refs if r.cell in height), default=0)
            if h > limits.gds_depth:
                raise LimitError(
                    f"references nest more than {limits.gds_depth} deep below cell {quote(name)}"
                )
            height[name] = h
            stack.pop()
            path.pop()
            on_path.discard(name)


# Element records: BOUNDARY, PATH, SREF, AREF, TEXT, NODE, BOX.
_ELEMENTS = {
    0x08: "shape",
    0x09: "path",
    0x0A: "sref",
    0x0B: "aref",
    0x0C: "text",
    0x15: "node",
    0x2D: "shape",
}


def parse_gds(data: bytes, limits: Limits = LIMITS) -> GdsLibrary:
    """Parse GDSII bytes. Raises :class:`GdsError` on a malformed stream,
    :class:`GdsCycleError` when a cell contains itself and :class:`LimitError` when
    the file has too many records, elements or cells or nests references too deep."""
    cells: dict[str, _Cell] = {}
    um_per_dbu = 0.0
    cell: _Cell | None = None
    element: str | None = None
    xy: list[Point] = []
    width = 0.0
    pathtype = 0
    ext = [0.0, 0.0]
    sname = ""
    strans = 0
    mag = 1.0
    angle = 0.0
    colrow = (1, 1)
    text = ""
    pos = 0
    records = 0
    elements = 0
    ended = False
    while pos < len(data):
        if pos + 4 > len(data):
            raise GdsError(f"truncated record header at byte {pos}")
        length, rtype, _dtype = struct.unpack(">HBB", data[pos : pos + 4])
        if length < 4 or pos + length > len(data):
            raise GdsError(f"bad record length {length} at byte {pos}")
        body = data[pos + 4 : pos + length]
        pos += length
        records += 1
        if records > limits.gds_records:
            raise LimitError(f"the GDS has more than {limits.gds_records} records")
        if rtype == 0x03:  # UNITS
            um_per_dbu = _real8(_need(body, 16, "UNITS")[8:16]) * 1e6
        elif rtype == 0x05:  # BGNSTR
            cell = None
        elif rtype == 0x06:  # STRNAME
            name = body.rstrip(b"\0").decode("latin-1")
            if name in cells:
                raise GdsError(f"cell {name} is defined twice")
            if len(cells) >= limits.gds_cells:
                raise LimitError(f"the GDS has more than {limits.gds_cells} cells")
            cell = cells[name] = _Cell(name)
        elif rtype == 0x07:  # ENDSTR
            cell = None
        elif rtype in _ELEMENTS:
            if cell is None:
                raise GdsError(f"element outside a cell at byte {pos - length}")
            elements += 1
            if elements > limits.gds_elements:
                raise LimitError(f"the GDS has more than {limits.gds_elements} elements")
            element = _ELEMENTS[rtype]
            xy, width, pathtype, ext = [], 0.0, 0, [0.0, 0.0]
            sname, strans, mag, angle, colrow, text = "", 0, 1.0, 0.0, (1, 1), ""
        elif rtype == 0x10:  # XY
            if len(body) % 8:
                raise GdsError("XY record has an odd number of coordinates")
            vals = struct.unpack(f">{len(body) // 4}i", body)
            xy = [(float(vals[i]), float(vals[i + 1])) for i in range(0, len(vals), 2)]
        elif rtype == 0x0F:  # WIDTH
            width = float(struct.unpack(">i", _need(body, 4, "WIDTH"))[0])
        elif rtype == 0x21:  # PATHTYPE
            pathtype = struct.unpack(">h", _need(body, 2, "PATHTYPE"))[0]
        elif rtype == 0x30:  # BGNEXTN
            ext[0] = float(struct.unpack(">i", _need(body, 4, "BGNEXTN"))[0])
        elif rtype == 0x31:  # ENDEXTN
            ext[1] = float(struct.unpack(">i", _need(body, 4, "ENDEXTN"))[0])
        elif rtype == 0x12:  # SNAME
            sname = body.rstrip(b"\0").decode("latin-1")
        elif rtype == 0x1A:  # STRANS
            strans = struct.unpack(">H", _need(body, 2, "STRANS"))[0]
        elif rtype == 0x1B:  # MAG
            mag = _real8(_need(body, 8, "MAG"))
        elif rtype == 0x1C:  # ANGLE
            angle = _real8(_need(body, 8, "ANGLE"))
        elif rtype == 0x13:  # COLROW
            cols, rows = struct.unpack(">hh", _need(body, 4, "COLROW"))
            colrow = (cols, rows)
        elif rtype == 0x19:  # STRING
            text = body.rstrip(b"\0").decode("latin-1")
        elif rtype == 0x11:  # ENDEL
            if cell is None or element is None:
                raise GdsError(f"ENDEL outside an element at byte {pos - length}")
            _finish(
                cell,
                element,
                xy,
                width,
                pathtype,
                (ext[0], ext[1]),
                sname,
                strans,
                mag,
                angle,
                colrow,
                text,
            )
            element = None
        elif rtype == 0x04:  # ENDLIB
            ended = True
            break
    if not ended:
        raise GdsError("no ENDLIB record: the file is truncated")
    if um_per_dbu <= 0:
        raise GdsError("no UNITS record")
    _check_hierarchy(cells, limits)
    return GdsLibrary(um_per_dbu, cells, limits)


def _add_shape(cell: _Cell, pts: list[Point]) -> None:
    cell.points.extend(chain.from_iterable(pts))
    cell.box = _union(cell.box, _points_box(pts))


def _finish(
    cell: _Cell,
    element: str,
    xy: list[Point],
    width: float,
    pathtype: int,
    ext: tuple[float, float],
    sname: str,
    strans: int,
    mag: float,
    angle: float,
    colrow: tuple[int, int],
    text: str,
) -> None:
    if element == "text":
        cell.labels.append(text)
        return
    if element == "node":
        return
    if not xy:
        raise GdsError(f"element without coordinates in cell {cell.name}")
    if element == "shape":
        _add_shape(cell, xy)
    elif element == "path":
        x0, y0, x1, y1 = _path_box(xy, width, pathtype, ext)
        _add_shape(cell, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    elif element == "sref":
        x, y = xy[0]
        cell.refs.append(_Ref(sname, x, y, bool(strans & 0x8000), mag, angle))
    elif element == "aref":
        if len(xy) < 3:
            raise GdsError(f"array reference without three points in cell {cell.name}")
        cols, rows = colrow
        if cols < 1 or rows < 1:
            raise GdsError(f"array reference with {cols} x {rows} instances in cell {cell.name}")
        (x0, y0), (x1, y1), (x2, y2) = xy[:3]
        cell.refs.append(
            _Ref(
                sname,
                x0,
                y0,
                bool(strans & 0x8000),
                mag,
                angle,
                cols,
                rows,
                ((x1 - x0) / cols, (y1 - y0) / cols),
                ((x2 - x0) / rows, (y2 - y0) / rows),
            )
        )


def structure_identities(data: bytes) -> list[tuple[bytes, str]]:
    """Each structure of a GDS stream: its name as written (one NUL pads an odd length)
    and its identity, what macro/v1's ``pdk.cell_name`` compares (a chip's merges compare
    layouts instead, ``chipleap.chip.merge.gds_names``). The identity is the file's UNITS record and the
    SHA-256 of the structure's records from BGNSTR to ENDSTR, each with its four-byte
    header, BGNSTR's body (the dates) left out: every other record a tool reads, its
    STRCLASS, properties and the order of its elements, counts. In a file whose UNITS
    record is not its only one, before its first structure, every identity is "",
    which matches nothing. Raises :class:`GdsError` when a record runs past the end of
    the stream."""
    pos, n = 0, len(data)
    units: list[bytes] = []
    units_first = False  # the first UNITS record came before any BGNSTR
    started = False  # a BGNSTR was seen
    out: list[tuple[bytes, str]] = []
    h = None
    name: bytes | None = None
    while pos + 4 <= n:
        length, rtype = (data[pos] << 8) | data[pos + 1], data[pos + 2]
        if length < 4 or pos + length > n:
            raise GdsError(f"bad record length {length} at byte {pos}")
        record = data[pos : pos + length]
        pos += length
        if rtype == 0x03:  # UNITS
            units_first = units_first or (not units and not started)
            units.append(record)
        elif rtype == 0x05:  # BGNSTR
            h, name, started = hashlib.sha256(record[:4]), None, True
            continue
        if h is not None:
            h.update(record)
            if rtype == 0x06 and name is None:  # STRNAME
                body = record[4:]
                name = body[:-1] if body.endswith(b"\0") else body
            if rtype == 0x07:  # ENDSTR
                if name is not None:
                    out.append((name, h.hexdigest()))
                h = None
        if rtype == 0x04:  # ENDLIB
            break
    if len(units) != 1 or not units_first:
        return [(name, "") for name, _ in out]
    return [(name, f"{units[0].hex()}:{digest}") for name, digest in out]


def read_gds(path: str | Path, limits: Limits = LIMITS) -> GdsLibrary:
    """Read and parse a GDS file; :class:`LimitError` when it is larger than the limit."""
    return parse_gds(read_capped(Path(path), limits.gds_bytes), limits)
