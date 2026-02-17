from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from cad2d_ir.schema import validate_ir

DXFPair = tuple[int, str]

_INSUNITS_TO_IR = {
    1: "inch",
    2: "ft",
    4: "mm",
    5: "cm",
    6: "m",
}

_IR_TO_INSUNITS = {
    "inch": 1,
    "ft": 2,
    "mm": 4,
    "cm": 5,
    "m": 6,
}

_TEXT_HALIGN_TO_DXF = {"left": 0, "center": 1, "right": 2}
_TEXT_VALIGN_TO_DXF = {"baseline": 0, "bottom": 1, "middle": 2, "top": 3}
_TEXT_HALIGN_FROM_DXF = {value: key for key, value in _TEXT_HALIGN_TO_DXF.items()}
_TEXT_VALIGN_FROM_DXF = {value: key for key, value in _TEXT_VALIGN_TO_DXF.items()}

_MTEXT_ATTACH_TO_DXF = {
    "top_left": 1,
    "top_center": 2,
    "top_right": 3,
    "middle_left": 4,
    "middle_center": 5,
    "middle_right": 6,
    "bottom_left": 7,
    "bottom_center": 8,
    "bottom_right": 9,
}
_MTEXT_ATTACH_FROM_DXF = {value: key for key, value in _MTEXT_ATTACH_TO_DXF.items()}

_DIM_KIND_TO_DXF = {
    "LINEAR": 0,
    "ALIGNED": 1,
    "ANGULAR": 2,
    "DIAMETER": 3,
    "RADIAL": 4,
    "ORDINATE": 6,
}

_DIM_KIND_FROM_DXF = {
    0: "LINEAR",
    1: "ALIGNED",
    2: "ANGULAR",
    3: "DIAMETER",
    4: "RADIAL",
    5: "ANGULAR",
    6: "ORDINATE",
}


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def read_dxf_file(
    path: str | Path,
    *,
    ir_version: str = "0.1.0",
    validate: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return dxf_to_ir(
        Path(path).read_text(encoding="utf-8"),
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
    )


def write_dxf_file(
    ir_document: dict[str, Any],
    path: str | Path,
    *,
    validate: bool = True,
    warnings: list[str] | None = None,
) -> None:
    Path(path).write_text(ir_to_dxf(ir_document, validate=validate, warnings=warnings), encoding="utf-8")


def dxf_to_ir(
    dxf_text: str,
    *,
    ir_version: str = "0.1.0",
    validate: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    pairs = _parse_pairs(dxf_text)
    sections = _split_sections(pairs)

    units = _header_units(sections.get("HEADER", []))
    entities = _entities_to_ir(sections.get("ENTITIES", []), warnings=warnings)
    blocks = _blocks_to_ir(sections.get("BLOCKS", []), warnings=warnings)

    ir_document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": ir_version,
        "header": {
            "units": units,
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": entities,
    }
    if blocks:
        ir_document["tables"] = {"blocks": blocks}

    if validate:
        validate_ir(ir_document)

    return ir_document


def ir_to_dxf(
    ir_document: dict[str, Any],
    *,
    validate: bool = True,
    warnings: list[str] | None = None,
) -> str:
    if validate:
        validate_ir(ir_document)

    header = ir_document.get("header", {})
    units = _IR_TO_INSUNITS.get(header.get("units", "mm"), 4)

    pairs: list[DXFPair] = []
    pairs.extend([(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1024"), (9, "$INSUNITS"), (70, str(units)), (0, "ENDSEC")])
    blocks = ir_document.get("tables", {}).get("blocks", {})
    if isinstance(blocks, dict) and blocks:
        pairs.extend(_blocks_to_pairs(blocks, warnings=warnings))

    constraints = ir_document.get("constraints")
    if isinstance(constraints, list) and constraints:
        _warn(warnings, "IR constraints are not representable in plain DXF and were omitted.")
    pairs.extend([(0, "SECTION"), (2, "ENTITIES")])

    for entity in ir_document.get("entities", []):
        pairs.extend(_entity_to_pairs(entity, warnings=warnings))

    pairs.extend([(0, "ENDSEC"), (0, "EOF")])
    return _format_pairs(pairs)


def _parse_pairs(dxf_text: str) -> list[DXFPair]:
    raw_lines = dxf_text.splitlines()
    if len(raw_lines) % 2 != 0:
        raise ValueError("DXF text must contain an even number of lines")

    pairs: list[DXFPair] = []
    for index in range(0, len(raw_lines), 2):
        code_text = raw_lines[index].strip()
        value = raw_lines[index + 1].rstrip("\r\n")
        try:
            code = int(code_text)
        except ValueError as exc:
            raise ValueError(f"Invalid DXF group code at line {index + 1}: {code_text}") from exc
        pairs.append((code, value))
    return pairs


def _split_sections(pairs: list[DXFPair]) -> dict[str, list[DXFPair]]:
    sections: dict[str, list[DXFPair]] = {}
    index = 0
    while index < len(pairs):
        code, value = pairs[index]
        if code == 0 and value.strip().upper() == "SECTION":
            if index + 1 >= len(pairs) or pairs[index + 1][0] != 2:
                raise ValueError("Malformed DXF SECTION record")
            name = pairs[index + 1][1].strip().upper()
            index += 2
            content: list[DXFPair] = []
            while index < len(pairs):
                if pairs[index][0] == 0 and pairs[index][1].strip().upper() == "ENDSEC":
                    index += 1
                    break
                content.append(pairs[index])
                index += 1
            sections[name] = content
            continue
        index += 1
    return sections


def _header_units(header_pairs: list[DXFPair]) -> str:
    current_name: str | None = None
    variables: dict[str, list[DXFPair]] = {}

    for code, value in header_pairs:
        if code == 9:
            current_name = value.strip().upper()
            variables[current_name] = []
            continue
        if current_name is not None:
            variables[current_name].append((code, value))

    if "$INSUNITS" not in variables:
        return "mm"
    insunits = _first_int(variables["$INSUNITS"], 70, default=4)
    return _INSUNITS_TO_IR.get(insunits, "mm")


def _entities_to_ir(entity_pairs: list[DXFPair], *, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    records = _pairs_to_records(entity_pairs)
    return _records_to_entities(records, warnings=warnings, context="ENTITIES")


def _blocks_to_ir(
    block_pairs: list[DXFPair],
    *,
    warnings: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    records = _pairs_to_records(block_pairs)
    blocks: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(records):
        kind, pairs = records[index]
        if kind != "BLOCK":
            index += 1
            continue

        name = _first_string(pairs, 2) or _first_string(pairs, 3)
        if not name:
            index += 1
            continue

        base_point = [
            _first_float(pairs, 10, default=0.0) or 0.0,
            _first_float(pairs, 20, default=0.0) or 0.0,
        ]

        index += 1
        entity_records: list[tuple[str, list[DXFPair]]] = []
        while index < len(records):
            inner_kind, inner_pairs = records[index]
            if inner_kind == "ENDBLK":
                index += 1
                break
            entity_records.append((inner_kind, inner_pairs))
            index += 1

        blocks[name] = {
            "base_point": base_point,
            "entities": _records_to_entities(
                entity_records,
                warnings=warnings,
                context=f"BLOCK:{name}",
            ),
        }
    return blocks


def _pairs_to_records(pairs: list[DXFPair]) -> list[tuple[str, list[DXFPair]]]:
    records: list[tuple[str, list[DXFPair]]] = []
    current_kind: str | None = None
    current_pairs: list[DXFPair] = []

    for code, value in pairs:
        if code == 0:
            if current_kind is not None:
                records.append((current_kind, current_pairs))
            current_kind = value.strip().upper()
            current_pairs = []
        elif current_kind is not None:
            current_pairs.append((code, value))

    if current_kind is not None:
        records.append((current_kind, current_pairs))
    return records


def _records_to_entities(
    records: list[tuple[str, list[DXFPair]]],
    *,
    warnings: list[str] | None = None,
    context: str = "ENTITIES",
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    record_index = 0
    serial = 1

    while record_index < len(records):
        kind, pairs = records[record_index]
        consumed = 1
        entity: dict[str, Any] | None = None

        if kind == "LINE":
            entity = _line_from_pairs(pairs, serial)
        elif kind == "CIRCLE":
            entity = _circle_from_pairs(pairs, serial)
        elif kind == "ARC":
            entity = _arc_from_pairs(pairs, serial)
        elif kind == "LWPOLYLINE":
            entity = _lwpolyline_from_pairs(pairs, serial)
        elif kind == "TEXT":
            entity = _text_from_pairs(pairs, serial)
        elif kind == "MTEXT":
            entity = _mtext_from_pairs(pairs, serial)
        elif kind == "INSERT":
            attributes: dict[str, str] = {}
            lookahead = record_index + 1
            while lookahead < len(records) and records[lookahead][0] == "ATTRIB":
                attrib_pairs = records[lookahead][1]
                tag = _first_string(attrib_pairs, 2)
                if tag:
                    attributes[tag] = _first_string(attrib_pairs, 1) or ""
                lookahead += 1
            if lookahead < len(records) and records[lookahead][0] == "SEQEND":
                lookahead += 1
            consumed = max(1, lookahead - record_index)
            entity = _insert_from_pairs(pairs, serial, attributes if attributes else None)
        elif kind == "HATCH":
            entity = _hatch_from_pairs(pairs, serial)
        elif kind == "SPLINE":
            entity = _spline_from_pairs(pairs, serial)
        elif kind == "DIMENSION":
            entity = _dimension_from_pairs(pairs, serial, warnings=warnings)

        if entity is not None:
            entities.append(entity)
            serial += 1
        else:
            _warn(warnings, f"[{context}] Unsupported DXF entity skipped: {kind}")
        record_index += consumed

    return entities


def _common_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    handle = _first_string(pairs, 5)
    base = f"E{handle}" if handle else f"E{idx}"
    clean_id = re.sub(r"[^A-Za-z0-9_.:\-]", "_", base)[:64]
    if not clean_id or not clean_id[0].isalpha():
        clean_id = f"E{clean_id}"

    entity: dict[str, Any] = {"id": clean_id}
    layer = _first_string(pairs, 8)
    if layer:
        entity["layer"] = layer

    linetype = _first_string(pairs, 6)
    if linetype:
        entity["linetype"] = linetype

    true_color = _first_int(pairs, 420, default=None)
    if true_color is not None:
        entity["color"] = f"#{true_color:06X}"
    else:
        aci = _first_int(pairs, 62, default=None)
        if aci is not None:
            entity["color"] = abs(aci)
            if aci < 0:
                entity["visible"] = False

    invisible = _first_int(pairs, 60, default=None)
    if invisible == 1:
        entity["visible"] = False

    lineweight = _first_int(pairs, 370, default=None)
    if lineweight is not None and lineweight >= 0:
        entity["lineweight_mm"] = round(lineweight / 100.0, 6)
    return entity


def _line_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LINE"
    entity["p1"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["p2"] = [_required_float(pairs, 11), _required_float(pairs, 21)]
    return entity


def _circle_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "CIRCLE"
    entity["center"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["radius"] = _required_float(pairs, 40)
    return entity


def _arc_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "ARC"
    entity["center"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["radius"] = _required_float(pairs, 40)
    entity["start_angle"] = _required_float(pairs, 50)
    entity["end_angle"] = _required_float(pairs, 51)
    return entity


def _lwpolyline_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LWPOLYLINE"

    vertices: list[list[float]] = []
    current: list[float] | None = None
    for code, value in pairs:
        if code == 10:
            if current is not None:
                vertices.append(current)
            current = [_to_float(value), 0.0]
        elif code == 20 and current is not None:
            current[1] = _to_float(value)
        elif code == 42 and current is not None:
            bulge = _to_float(value)
            if len(current) == 2:
                current.append(bulge)
            else:
                current[2] = bulge
    if current is not None:
        vertices.append(current)

    if len(vertices) < 2:
        raise ValueError("LWPOLYLINE requires at least 2 vertices")

    flags = _first_int(pairs, 70, default=0)
    entity["vertices"] = vertices
    if flags & 1:
        entity["closed"] = True
    return entity


def _text_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "TEXT"
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["height"] = _required_float(pairs, 40)
    entity["text"] = _first_string(pairs, 1) or ""

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    style = _first_string(pairs, 7)
    if style:
        entity["style"] = style

    halign = _TEXT_HALIGN_FROM_DXF.get(_first_int(pairs, 72, default=0), "left")
    valign = _TEXT_VALIGN_FROM_DXF.get(_first_int(pairs, 73, default=0), "baseline")
    if halign != "left":
        entity["halign"] = halign
    if valign != "baseline":
        entity["valign"] = valign

    width_factor = _first_float(pairs, 41, default=None)
    if width_factor is not None:
        entity["width_factor"] = width_factor

    oblique = _first_float(pairs, 51, default=None)
    if oblique is not None:
        entity["oblique_deg"] = oblique
    return entity


def _mtext_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "MTEXT"
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["height"] = _required_float(pairs, 40)

    chunks: list[str] = []
    for code, value in pairs:
        if code in (3, 1):
            chunks.append(value)
    entity["text"] = "".join(chunks)

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    width = _first_float(pairs, 41, default=None)
    if width is not None:
        entity["width"] = width

    style = _first_string(pairs, 7)
    if style:
        entity["style"] = style

    attach = _MTEXT_ATTACH_FROM_DXF.get(_first_int(pairs, 71, default=1), "top_left")
    if attach != "top_left":
        entity["attach"] = attach
    return entity


def _insert_from_pairs(pairs: list[DXFPair], idx: int, attributes: dict[str, str] | None) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "INSERT"
    block_name = _first_string(pairs, 2)
    if not block_name:
        raise ValueError("INSERT requires block name (group code 2)")
    entity["block"] = block_name
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    sx = _first_float(pairs, 41, default=1.0) or 1.0
    sy = _first_float(pairs, 42, default=sx) or sx
    if abs(sx - sy) < 1e-12:
        if abs(sx - 1.0) > 1e-12:
            entity["scale"] = sx
    else:
        entity["scale"] = [sx, sy]

    if attributes:
        entity["attributes"] = attributes
    return entity


def _hatch_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "HATCH"

    solid = (_first_int(pairs, 70, default=1) or 1) == 1
    if not solid:
        entity["solid"] = False

    pattern = _first_string(pairs, 2)
    if pattern and pattern.upper() != "SOLID":
        entity["pattern"] = pattern

    loops = _parse_hatch_loops(pairs)
    if not loops:
        raise ValueError("HATCH requires at least one polyline loop")
    entity["loops"] = loops
    return entity


def _spline_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "SPLINE"

    degree = _first_int(pairs, 71, default=None)
    if degree is None:
        raise ValueError("SPLINE requires degree (group code 71)")
    entity["degree"] = degree

    control_points: list[list[float]] = []
    current: list[float] | None = None
    for code, value in pairs:
        if code == 10:
            if current is not None:
                control_points.append(current)
            current = [_to_float(value), 0.0]
        elif code == 20 and current is not None:
            current[1] = _to_float(value)
    if current is not None:
        control_points.append(current)
    if not control_points:
        raise ValueError("SPLINE requires at least one control point")
    entity["control_points"] = control_points

    knots = _all_floats(pairs, 40)
    if knots:
        entity["knots"] = knots

    weights = _all_floats(pairs, 41)
    if weights:
        entity["weights"] = weights

    flags = _first_int(pairs, 70, default=0) or 0
    if flags & 1:
        entity["closed"] = True
    return entity


def _dimension_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "DIMENSION"

    raw_type = _first_int(pairs, 70, default=0) or 0
    dim_type = raw_type & 7
    dim_kind = _DIM_KIND_FROM_DXF.get(dim_type)
    if dim_kind is None:
        dim_kind = "LINEAR"
        _warn(warnings, f"[DIMENSION] Unsupported dimension type {dim_type}; mapped to LINEAR.")
    entity["dim_kind"] = dim_kind

    style = _first_string(pairs, 3)
    if style:
        entity["style"] = style

    definition: dict[str, Any] = {}
    block_name = _first_string(pairs, 2)
    if block_name:
        definition["block"] = block_name

    text = _first_string(pairs, 1)
    if text is not None and text != "":
        definition["text"] = text

    points: dict[str, list[float]] = {}
    for key, x_code, y_code in (
        ("location", 10, 20),
        ("text_midpoint", 11, 21),
        ("p1", 13, 23),
        ("p2", 14, 24),
        ("p3", 15, 25),
        ("p4", 16, 26),
    ):
        x = _first_float(pairs, x_code, default=None)
        y = _first_float(pairs, y_code, default=None)
        if x is not None and y is not None:
            points[key] = [x, y]
    if points:
        definition["points"] = points

    rotation = _first_float(pairs, 50, default=None)
    if rotation is not None:
        definition["rotation"] = rotation

    text_rotation = _first_float(pairs, 53, default=None)
    if text_rotation is not None:
        definition["text_rotation"] = text_rotation

    measurement = _first_float(pairs, 42, default=None)
    if measurement is not None:
        definition["measurement"] = measurement

    entity["definition"] = definition
    return entity


def _parse_hatch_loops(pairs: list[DXFPair]) -> list[dict[str, Any]]:
    loop_count_index = next((i for i, (code, _) in enumerate(pairs) if code == 91), None)
    if loop_count_index is None:
        return []

    num_loops = int(float(pairs[loop_count_index][1]))
    cursor = loop_count_index + 1
    loops: list[dict[str, Any]] = []

    for loop_index in range(num_loops):
        while cursor < len(pairs) and pairs[cursor][0] != 92:
            cursor += 1
        if cursor >= len(pairs):
            break

        path_flags = int(float(pairs[cursor][1]))
        cursor += 1
        if not (path_flags & 2):
            while cursor < len(pairs) and pairs[cursor][0] not in {92, 75, 76, 98}:
                cursor += 1
            continue

        has_bulge = 0
        while cursor < len(pairs):
            code, value = pairs[cursor]
            if code == 72:
                has_bulge = int(float(value))
                cursor += 1
            elif code == 73:
                cursor += 1
            elif code == 93:
                vertex_count = int(float(value))
                cursor += 1
                break
            else:
                cursor += 1
        else:
            break

        vertices: list[list[float]] = []
        for _ in range(vertex_count):
            x: float | None = None
            y: float | None = None
            bulge: float | None = None

            while cursor < len(pairs):
                code, value = pairs[cursor]
                if code == 10 and x is None:
                    x = _to_float(value)
                    cursor += 1
                    continue
                if code == 20 and y is None:
                    y = _to_float(value)
                    cursor += 1
                    continue
                if code == 42 and has_bulge:
                    bulge = _to_float(value)
                    cursor += 1
                    continue
                if x is not None and y is not None:
                    break
                cursor += 1

            if x is None or y is None:
                break
            if has_bulge and bulge is not None:
                vertices.append([x, y, bulge])
            else:
                vertices.append([x, y])

        if len(vertices) >= 3:
            loops.append({"vertices": vertices, "is_outer": loop_index == 0})
    return loops


def _line_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    p1 = entity["p1"]
    p2 = entity["p2"]
    pairs = [(0, "LINE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend([(10, _num(p1[0])), (20, _num(p1[1])), (11, _num(p2[0])), (21, _num(p2[1]))])
    return pairs


def _circle_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    center = entity["center"]
    pairs = [(0, "CIRCLE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend([(10, _num(center[0])), (20, _num(center[1])), (40, _num(entity["radius"]))])
    return pairs


def _arc_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    center = entity["center"]
    pairs = [(0, "ARC")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(center[0])),
            (20, _num(center[1])),
            (40, _num(entity["radius"])),
            (50, _num(entity["start_angle"])),
            (51, _num(entity["end_angle"])),
        ]
    )
    return pairs


def _lwpolyline_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    vertices = entity["vertices"]
    pairs = [(0, "LWPOLYLINE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.append((90, str(len(vertices))))
    pairs.append((70, str(1 if entity.get("closed") else 0)))
    for vertex in vertices:
        pairs.append((10, _num(vertex[0])))
        pairs.append((20, _num(vertex[1])))
        if len(vertex) >= 3:
            pairs.append((42, _num(vertex[2])))
    return pairs


def _text_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "TEXT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(insert[0])),
            (20, _num(insert[1])),
            (40, _num(entity["height"])),
            (1, str(entity["text"])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    style = entity.get("style")
    if style:
        pairs.append((7, str(style)))

    halign = str(entity.get("halign", "left"))
    valign = str(entity.get("valign", "baseline"))
    halign_code = _TEXT_HALIGN_TO_DXF.get(halign, 0)
    valign_code = _TEXT_VALIGN_TO_DXF.get(valign, 0)
    if halign_code != 0:
        pairs.append((72, str(halign_code)))
    if valign_code != 0:
        pairs.append((73, str(valign_code)))
    if halign_code != 0 or valign_code != 0:
        pairs.append((11, _num(insert[0])))
        pairs.append((21, _num(insert[1])))

    width_factor = entity.get("width_factor")
    if width_factor is not None:
        pairs.append((41, _num(width_factor)))

    oblique = entity.get("oblique_deg")
    if oblique is not None:
        pairs.append((51, _num(oblique)))
    return pairs


def _mtext_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "MTEXT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(insert[0])),
            (20, _num(insert[1])),
            (40, _num(entity["height"])),
            (1, str(entity["text"])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    width = entity.get("width")
    if width is not None:
        pairs.append((41, _num(width)))

    style = entity.get("style")
    if style:
        pairs.append((7, str(style)))

    attach = entity.get("attach")
    if isinstance(attach, str):
        attach_code = _MTEXT_ATTACH_TO_DXF.get(attach)
        if attach_code is not None and attach_code != 1:
            pairs.append((71, str(attach_code)))
    return pairs


def _insert_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "INSERT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (2, str(entity["block"])),
            (10, _num(insert[0])),
            (20, _num(insert[1])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    scale = entity.get("scale", 1)
    if isinstance(scale, (int, float)):
        scale_value = float(scale)
        if abs(scale_value - 1.0) > 1e-12:
            pairs.append((41, _num(scale_value)))
            pairs.append((42, _num(scale_value)))
    elif isinstance(scale, list) and len(scale) == 2:
        pairs.append((41, _num(scale[0])))
        pairs.append((42, _num(scale[1])))

    attributes = entity.get("attributes")
    if isinstance(attributes, dict) and attributes:
        pairs.append((66, "1"))
        for tag, value in attributes.items():
            pairs.extend(
                [
                    (0, "ATTRIB"),
                    (8, str(entity.get("layer", "0"))),
                    (10, _num(insert[0])),
                    (20, _num(insert[1])),
                    (40, "1"),
                    (2, str(tag)),
                    (1, str(value)),
                    (70, "0"),
                ]
            )
        pairs.extend([(0, "SEQEND"), (8, str(entity.get("layer", "0")))])
    return pairs


def _hatch_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    loops = entity["loops"]
    if not isinstance(loops, list) or not loops:
        raise ValueError("HATCH requires non-empty loops")

    solid = bool(entity.get("solid", True))
    pattern = str(entity.get("pattern", "SOLID")) if not solid else "SOLID"

    pairs = [(0, "HATCH")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, "0"),
            (20, "0"),
            (30, "0"),
            (210, "0"),
            (220, "0"),
            (230, "1"),
            (2, pattern),
            (70, "1" if solid else "0"),
            (71, "0"),
            (91, str(len(loops))),
        ]
    )

    for loop in loops:
        vertices = loop["vertices"]
        has_bulge = any(len(vertex) >= 3 for vertex in vertices)
        pairs.extend(
            [
                (92, "2"),
                (72, "1" if has_bulge else "0"),
                (73, "1"),
                (93, str(len(vertices))),
            ]
        )
        for vertex in vertices:
            pairs.append((10, _num(vertex[0])))
            pairs.append((20, _num(vertex[1])))
            if has_bulge:
                pairs.append((42, _num(vertex[2] if len(vertex) >= 3 else 0.0)))

    pairs.extend([(75, "0"), (76, "1")])
    if not solid:
        pairs.append((78, "0"))
    pairs.append((98, "0"))
    return pairs


def _spline_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    control_points = entity["control_points"]
    degree = int(entity["degree"])
    knots = entity.get("knots")
    if not isinstance(knots, list) or not knots:
        knots = _default_spline_knots(len(control_points), degree)

    pairs = [(0, "SPLINE")]
    pairs.extend(_common_to_pairs(entity))
    flags = 1 if entity.get("closed") else 0
    pairs.extend(
        [
            (70, str(flags)),
            (71, str(degree)),
            (72, str(len(knots))),
            (73, str(len(control_points))),
            (74, "0"),
        ]
    )

    for knot in knots:
        pairs.append((40, _num(knot)))

    for point in control_points:
        pairs.append((10, _num(point[0])))
        pairs.append((20, _num(point[1])))

    weights = entity.get("weights")
    if isinstance(weights, list):
        for weight in weights:
            pairs.append((41, _num(weight)))
    return pairs


def _dimension_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    definition = entity.get("definition")
    if not isinstance(definition, dict):
        definition = {}

    pairs = [(0, "DIMENSION")]
    pairs.extend(_common_to_pairs(entity))

    block_name = definition.get("block", "*D0")
    pairs.append((2, str(block_name)))

    style = entity.get("style", "Standard")
    pairs.append((3, str(style)))

    dim_kind = str(entity.get("dim_kind", "LINEAR"))
    dim_code = _DIM_KIND_TO_DXF.get(dim_kind, 0)
    pairs.append((70, str(dim_code)))

    points = definition.get("points")
    if not isinstance(points, dict):
        points = {}

    location = _dimension_point(points, definition, "location", default=[0.0, 0.0])
    pairs.append((10, _num(location[0])))
    pairs.append((20, _num(location[1])))
    pairs.append((30, "0"))

    text_midpoint = _dimension_point(points, definition, "text_midpoint", default=None)
    if text_midpoint is not None:
        pairs.append((11, _num(text_midpoint[0])))
        pairs.append((21, _num(text_midpoint[1])))
        pairs.append((31, "0"))

    for key, x_code, y_code in (("p1", 13, 23), ("p2", 14, 24), ("p3", 15, 25), ("p4", 16, 26)):
        point = _dimension_point(points, definition, key, default=None)
        if point is not None:
            pairs.append((x_code, _num(point[0])))
            pairs.append((y_code, _num(point[1])))

    text = definition.get("text")
    if text is not None:
        pairs.append((1, str(text)))

    rotation = definition.get("rotation")
    if isinstance(rotation, (int, float)):
        pairs.append((50, _num(rotation)))

    text_rotation = definition.get("text_rotation")
    if isinstance(text_rotation, (int, float)):
        pairs.append((53, _num(text_rotation)))

    measurement = definition.get("measurement")
    if isinstance(measurement, (int, float)):
        pairs.append((42, _num(measurement)))
    return pairs


def _entity_to_pairs(entity: dict[str, Any], *, warnings: list[str] | None = None) -> list[DXFPair]:
    kind = entity.get("kind")
    if kind == "LINE":
        return _line_to_pairs(entity)
    if kind == "CIRCLE":
        return _circle_to_pairs(entity)
    if kind == "ARC":
        return _arc_to_pairs(entity)
    if kind == "LWPOLYLINE":
        return _lwpolyline_to_pairs(entity)
    if kind == "TEXT":
        return _text_to_pairs(entity)
    if kind == "MTEXT":
        return _mtext_to_pairs(entity)
    if kind == "INSERT":
        return _insert_to_pairs(entity)
    if kind == "HATCH":
        return _hatch_to_pairs(entity)
    if kind == "SPLINE":
        return _spline_to_pairs(entity)
    if kind == "DIMENSION":
        return _dimension_to_pairs(entity)
    raise ValueError(f"Unsupported IR entity kind for DXF writer: {kind}")


def _blocks_to_pairs(blocks: dict[str, Any], *, warnings: list[str] | None = None) -> list[DXFPair]:
    pairs: list[DXFPair] = [(0, "SECTION"), (2, "BLOCKS")]
    for name, definition in blocks.items():
        base = definition.get("base_point", [0.0, 0.0])
        pairs.extend(
            [
                (0, "BLOCK"),
                (8, "0"),
                (2, str(name)),
                (70, "0"),
                (10, _num(base[0])),
                (20, _num(base[1])),
                (30, "0"),
                (3, str(name)),
                (1, ""),
            ]
        )

        for entity in definition.get("entities", []):
            pairs.extend(_entity_to_pairs(entity, warnings=warnings))

        pairs.extend([(0, "ENDBLK"), (8, "0")])
    pairs.append((0, "ENDSEC"))
    return pairs


def _common_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    pairs: list[DXFPair] = []
    layer = entity.get("layer", "0")
    pairs.append((8, str(layer)))

    linetype = entity.get("linetype")
    if linetype:
        pairs.append((6, str(linetype)))

    color = entity.get("color")
    if isinstance(color, int):
        pairs.append((62, str(color)))
    elif isinstance(color, str) and re.fullmatch(r"#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{8})", color):
        pairs.append((420, str(int(color[1:7], 16))))

    lineweight = entity.get("lineweight_mm")
    if isinstance(lineweight, (int, float)):
        pairs.append((370, str(int(round(float(lineweight) * 100.0)))))

    if entity.get("visible") is False:
        pairs.append((60, "1"))
    return pairs


def _dimension_point(
    points: dict[str, Any],
    definition: dict[str, Any],
    key: str,
    *,
    default: list[float] | None,
) -> list[float] | None:
    candidate = points.get(key, definition.get(key))
    if isinstance(candidate, list) and len(candidate) >= 2:
        return [float(candidate[0]), float(candidate[1])]
    return default


def _default_spline_knots(control_point_count: int, degree: int) -> list[float]:
    total = control_point_count + degree + 1
    edge = degree + 1
    interior = total - 2 * edge
    if interior < 0:
        raise ValueError("Invalid SPLINE definition: too few control points for the degree")

    knots = [0.0] * edge
    if interior > 0:
        step = 1.0 / (interior + 1)
        knots.extend(step * (index + 1) for index in range(interior))
    knots.extend([1.0] * edge)
    return knots


def _required_float(pairs: list[DXFPair], code: int) -> float:
    value = _first_float(pairs, code, default=None)
    if value is None:
        raise ValueError(f"Missing required DXF group code: {code}")
    return value


def _first_string(pairs: list[DXFPair], code: int) -> str | None:
    for pair_code, value in pairs:
        if pair_code == code:
            return value.strip()
    return None


def _first_int(pairs: list[DXFPair], code: int, default: int | None) -> int | None:
    value = _first_string(pairs, code)
    if value is None:
        return default
    return int(float(value))


def _first_float(pairs: list[DXFPair], code: int, default: float | None) -> float | None:
    value = _first_string(pairs, code)
    if value is None:
        return default
    return _to_float(value)


def _all_floats(pairs: list[DXFPair], code: int) -> list[float]:
    values: list[float] = []
    for pair_code, value in pairs:
        if pair_code == code:
            values.append(_to_float(value))
    return values


def _to_float(value: str) -> float:
    return float(value.strip())


def _num(value: Any) -> str:
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    text = f"{as_float:.12g}"
    return "0" if text == "-0" else text


def _format_pairs(pairs: list[DXFPair]) -> str:
    lines: list[str] = []
    for code, value in pairs:
        lines.append(str(code))
        lines.append(str(value))
    return "\n".join(lines) + "\n"
