"""Just enough GDSII to check a macro: cell names, top cells, bounding boxes, labels.

The reader walks the record stream once, keeps each cell's shapes as bounding boxes
and its references, and computes a cell's box through its whole hierarchy on demand.
Text does not count toward a box, as in KLayout. It has no dependencies, so ChipLeap
and aispice (``aispice_core::macropkg::gds``) read GDS the same way.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

Box = tuple[float, float, float, float]


class GdsError(ValueError):
    pass


@dataclass
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


@dataclass
class _Cell:
    name: str
    # Each shape's corner points: a polygon's vertices, a path's box corners.
    shapes: list[list[tuple[float, float]]] = field(default_factory=list)
    refs: list[_Ref] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


@dataclass
class GdsLibrary:
    """A parsed GDS file. Coordinates are in micrometres."""

    um_per_dbu: float
    cells: dict[str, _Cell]

    def top_cells(self) -> list[str]:
        """Cells no other cell references, in file order."""
        used = {r.cell for c in self.cells.values() for r in c.refs}
        return [n for n in self.cells if n not in used]

    def labels(self, cell: str) -> list[str]:
        """Text strings placed directly in a cell."""
        return list(self.cells[cell].labels)

    def bbox(self, cell: str) -> Box | None:
        """The cell's bounding box through its hierarchy, or None when it is empty."""
        return _scale(self._bbox_dbu(cell, ()), self.um_per_dbu)

    def _cell(self, name: str, stack: tuple[str, ...]) -> _Cell:
        if name in stack:
            raise GdsError(f"cell {name} contains itself")
        cell = self.cells.get(name)
        if cell is None:
            raise GdsError(f"cell {name} is referenced but not defined")
        return cell

    def _bbox_dbu(self, name: str, stack: tuple[str, ...]) -> Box | None:
        cell = self._cell(name, stack)
        box: Box | None = None
        for pts in cell.shapes:
            box = _union(box, _points_box(pts))
        for r in cell.refs:
            for dx, dy in _corners(r):
                if r.angle % 90 == 0:
                    # A quarter turn maps the child's box onto the placed box exactly.
                    child = self._bbox_dbu(r.cell, (*stack, name))
                    if child is not None:
                        box = _union(box, _transform(child, r, dx, dy))
                else:
                    placed = self._exact(r.cell, _affine(r, dx, dy), (*stack, name))
                    if placed is not None:
                        box = _union(box, placed)
        return box

    def _exact(self, name: str, m: Affine, stack: tuple[str, ...]) -> Box | None:
        """The box of a cell under any transform, from its transformed corner points."""
        cell = self._cell(name, stack)
        box: Box | None = None
        for pts in cell.shapes:
            box = _union(box, _points_box([_apply(m, p) for p in pts]))
        for r in cell.refs:
            for dx, dy in _corners(r):
                placed = self._exact(r.cell, _compose(m, _affine(r, dx, dy)), (*stack, name))
                if placed is not None:
                    box = _union(box, placed)
        return box


# x' = a x + b y + e, y' = c x + d y + f
Affine = tuple[float, float, float, float, float, float]


def _corners(r: _Ref) -> list[tuple[float, float]]:
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


def _compose(m: Affine, n: Affine) -> Affine:
    """m after n."""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (
        a * p + b * r,
        a * q + b * s,
        c * p + d * r,
        c * q + d * s,
        a * t + b * u + e,
        c * t + d * u + f,
    )


def _apply(m: Affine, pt: tuple[float, float]) -> tuple[float, float]:
    x, y = pt
    return (m[0] * x + m[1] * y + m[4], m[2] * x + m[3] * y + m[5])


def _points_box(pts: list[tuple[float, float]]) -> Box:
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
    return _points_box(
        [_apply(m, p) for p in ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))]
    )


def _real8(b: bytes) -> float:
    sign = -1.0 if b[0] & 0x80 else 1.0
    exp = (b[0] & 0x7F) - 64
    mant = int.from_bytes(b[1:8], "big")
    return sign * mant / float(1 << 56) * 16.0**exp


def _path_box(
    xy: list[tuple[float, float]], width: float, pathtype: int, ext: tuple[float, float]
) -> Box:
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


def parse_gds(data: bytes) -> GdsLibrary:
    """Parse GDSII bytes. Raises :class:`GdsError` on a malformed stream."""
    cells: dict[str, _Cell] = {}
    um_per_dbu = 0.0
    cell: _Cell | None = None
    element: str | None = None
    xy: list[tuple[float, float]] = []
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
    ended = False
    while pos < len(data):
        if pos + 4 > len(data):
            raise GdsError(f"truncated record header at byte {pos}")
        length, rtype, _dtype = struct.unpack(">HBB", data[pos : pos + 4])
        if length < 4 or pos + length > len(data):
            raise GdsError(f"bad record length {length} at byte {pos}")
        body = data[pos + 4 : pos + length]
        pos += length
        if rtype == 0x03:  # UNITS
            if len(body) < 16:
                raise GdsError("UNITS record is too short")
            um_per_dbu = _real8(body[8:16]) * 1e6
        elif rtype == 0x05:  # BGNSTR
            cell = None
        elif rtype == 0x06:  # STRNAME
            name = body.rstrip(b"\0").decode("latin-1")
            if name in cells:
                raise GdsError(f"cell {name} is defined twice")
            cell = cells[name] = _Cell(name)
        elif rtype == 0x07:  # ENDSTR
            cell = None
        elif rtype in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x2D):
            # BOUNDARY, PATH, SREF, AREF, TEXT, BOX
            if cell is None:
                raise GdsError(f"element outside a cell at byte {pos - length}")
            element = {0x08: "shape", 0x09: "path", 0x0A: "sref", 0x0B: "aref", 0x0C: "text"}.get(
                rtype, "shape"
            )
            xy, width, pathtype, ext = [], 0.0, 0, [0.0, 0.0]
            sname, strans, mag, angle, colrow, text = "", 0, 1.0, 0.0, (1, 1), ""
        elif rtype == 0x10:  # XY
            if len(body) % 8:
                raise GdsError("XY record has an odd number of coordinates")
            vals = struct.unpack(f">{len(body) // 4}i", body)
            xy = [(float(vals[i]), float(vals[i + 1])) for i in range(0, len(vals), 2)]
        elif rtype == 0x0F:  # WIDTH
            width = float(struct.unpack(">i", body[:4])[0])
        elif rtype == 0x21:  # PATHTYPE
            pathtype = struct.unpack(">h", body[:2])[0]
        elif rtype == 0x30:  # BGNEXTN
            ext[0] = float(struct.unpack(">i", body[:4])[0])
        elif rtype == 0x31:  # ENDEXTN
            ext[1] = float(struct.unpack(">i", body[:4])[0])
        elif rtype == 0x12:  # SNAME
            sname = body.rstrip(b"\0").decode("latin-1")
        elif rtype == 0x1A:  # STRANS
            strans = struct.unpack(">H", body[:2])[0]
        elif rtype == 0x1B:  # MAG
            mag = _real8(body[:8])
        elif rtype == 0x1C:  # ANGLE
            angle = _real8(body[:8])
        elif rtype == 0x13:  # COLROW
            cols, rows = struct.unpack(">hh", body[:4])
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
    return GdsLibrary(um_per_dbu, cells)


def _finish(
    cell: _Cell,
    element: str,
    xy: list[tuple[float, float]],
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
    if not xy:
        raise GdsError(f"element without coordinates in cell {cell.name}")
    if element == "shape":
        cell.shapes.append(list(xy))
    elif element == "path":
        x0, y0, x1, y1 = _path_box(xy, width, pathtype, ext)
        cell.shapes.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
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


def read_gds(path: str | Path) -> GdsLibrary:
    return parse_gds(Path(path).read_bytes())
