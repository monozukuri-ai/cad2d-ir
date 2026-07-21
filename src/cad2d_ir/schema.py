from __future__ import annotations

import json
from importlib.resources import files
import re
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover - fallback path
    jsonschema = None


class IRValidationError(ValueError):
    """Raised when an IR document is invalid."""


_SUPPORTED_KINDS = {
    "LINE",
    "CIRCLE",
    "ARC",
    "POINT",
    "ELLIPSE",
    "LWPOLYLINE",
    "SPLINE",
    "TEXT",
    "MTEXT",
    "INSERT",
    "HATCH",
    "DIMENSION",
}


def _fallback_schema_path() -> Path:
    current = Path(__file__).resolve()
    candidates = [
        current.parents[2] / "ir_schema.json",
        current.parents[1] / "ir_schema.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("Could not locate ir_schema.json")


def _load_packaged_schema() -> str | None:
    try:
        resource = files("cad2d_ir.data").joinpath("ir_schema.json")
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def load_schema(path: str | Path | None = None) -> dict[str, Any]:
    """Load the IR JSON schema.

    Resolution order:
    1. Explicit path argument.
    2. Packaged schema data (installed package).
    3. Repository root fallback.
    """
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    packaged = _load_packaged_schema()
    if packaged is not None:
        return json.loads(packaged)

    schema_path = _fallback_schema_path()
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_ir(
    document: Any,
    schema: dict[str, Any] | None = None,
    *,
    strict_jsonschema: bool = False,
) -> None:
    """Validate an IR document.

    By default, validation uses a schema-informed fallback checker.
    Set strict_jsonschema=True to additionally run jsonschema validators.
    """
    _fallback_validate(document)

    if not strict_jsonschema or jsonschema is None:
        return

    if schema is None:
        schema = load_schema()

    if jsonschema is not None:
        for validator_cls in (
            jsonschema.Draft202012Validator,
            jsonschema.Draft7Validator,
        ):
            try:
                validator = validator_cls(schema)
                errors = sorted(
                    validator.iter_errors(document),
                    key=lambda err: list(err.absolute_path),
                )
            except Exception:
                continue

            if errors:
                error = errors[0]
                path = ".".join(str(item) for item in error.absolute_path)
                detail = f"{path}: {error.message}" if path else error.message
                raise IRValidationError(detail)
            return

    _fallback_validate(document)


def _fallback_validate(document: Any) -> None:
    if not isinstance(document, dict):
        raise IRValidationError("Root must be an object")

    for key in ("format", "version", "header", "entities"):
        if key not in document:
            raise IRValidationError(f"Missing required key: {key}")

    if document["format"] != "cad2d-ir":
        raise IRValidationError("format must be 'cad2d-ir'")

    version = document["version"]
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
    ):
        raise IRValidationError("version must be semantic version format X.Y.Z")

    header = document["header"]
    if not isinstance(header, dict):
        raise IRValidationError("header must be an object")

    units = header.get("units")
    if units not in {"mm", "cm", "m", "inch", "ft", "unitless", "unknown", "custom"}:
        raise IRValidationError("header.units is invalid")

    if units == "custom":
        scale = header.get("unit_scale_to_mm")
        if not isinstance(scale, (int, float)) or scale <= 0:
            raise IRValidationError(
                "header.unit_scale_to_mm must be > 0 for custom units"
            )

    if header.get("angle_unit") not in {"deg", "rad"}:
        raise IRValidationError("header.angle_unit must be 'deg' or 'rad'")

    if header.get("coord_space") not in {"world", "normalized"}:
        raise IRValidationError("header.coord_space must be 'world' or 'normalized'")

    _validate_tables(document.get("tables"))
    _validate_source(document.get("source"), "source")

    entities = document["entities"]
    if not isinstance(entities, list):
        raise IRValidationError("entities must be an array")

    for idx, entity in enumerate(entities):
        _validate_entity(entity, f"entities[{idx}]")

    _validate_constraints(document.get("constraints"))


def _validate_tables(tables: Any) -> None:
    if tables is None:
        return
    if not isinstance(tables, dict):
        raise IRValidationError("tables must be an object")

    blocks = tables.get("blocks")
    if blocks is None:
        return
    if not isinstance(blocks, dict):
        raise IRValidationError("tables.blocks must be an object")

    for block_name, block_def in blocks.items():
        if not isinstance(block_name, str) or not block_name:
            raise IRValidationError("tables.blocks keys must be non-empty strings")
        if not isinstance(block_def, dict):
            raise IRValidationError(f"tables.blocks.{block_name} must be an object")

        base_point = block_def.get("base_point")
        if not isinstance(base_point, list) or len(base_point) != 2:
            raise IRValidationError(
                f"tables.blocks.{block_name}.base_point must be [x, y]"
            )
        if not all(isinstance(v, (int, float)) for v in base_point):
            raise IRValidationError(
                f"tables.blocks.{block_name}.base_point must contain only numbers"
            )

        block_entities = block_def.get("entities")
        if not isinstance(block_entities, list):
            raise IRValidationError(
                f"tables.blocks.{block_name}.entities must be an array"
            )
        for idx, entity in enumerate(block_entities):
            _validate_entity(entity, f"tables.blocks.{block_name}.entities[{idx}]")


def _validate_entity(entity: Any, path: str) -> None:
    if not isinstance(entity, dict):
        raise IRValidationError(f"{path} must be an object")
    if "id" not in entity or "kind" not in entity:
        raise IRValidationError(f"{path} must contain id and kind")
    if not isinstance(entity["id"], str) or not entity["id"]:
        raise IRValidationError(f"{path}.id must be a non-empty string")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-:.]{0,63}", entity["id"]) is None:
        raise IRValidationError(f"{path}.id has invalid format")
    if entity["kind"] not in _SUPPORTED_KINDS:
        raise IRValidationError(f"{path}.kind is not supported")

    kind = entity["kind"]
    if kind == "LINE":
        _require_point2(entity, "p1", path)
        _require_point2(entity, "p2", path)
    elif kind == "CIRCLE":
        _require_point2(entity, "center", path)
        _require_positive_number(entity, "radius", path)
    elif kind == "ARC":
        _require_point2(entity, "center", path)
        _require_positive_number(entity, "radius", path)
        _require_number(entity, "start_angle", path)
        _require_number(entity, "end_angle", path)
    elif kind == "POINT":
        _require_point2(entity, "position", path)
        if "marker_code" in entity and not isinstance(entity["marker_code"], int):
            raise IRValidationError(f"{path}.marker_code must be an integer")
        if "rotation" in entity:
            _require_number(entity, "rotation", path)
        if "scale" in entity:
            _require_positive_number(entity, "scale", path)
        if "temporary" in entity and not isinstance(entity["temporary"], bool):
            raise IRValidationError(f"{path}.temporary must be boolean")
    elif kind == "ELLIPSE":
        _require_point2(entity, "center", path)
        _require_point2(entity, "major_axis", path)
        if (
            float(entity["major_axis"][0]) == 0.0
            and float(entity["major_axis"][1]) == 0.0
        ):
            raise IRValidationError(f"{path}.major_axis must be non-zero")
        _require_positive_number(entity, "ratio", path)
        if float(entity["ratio"]) > 1.0:
            raise IRValidationError(f"{path}.ratio must be <= 1")
        _require_number(entity, "start_param", path)
        _require_number(entity, "end_param", path)
        if "ccw" in entity and not isinstance(entity["ccw"], bool):
            raise IRValidationError(f"{path}.ccw must be boolean")
    elif kind == "LWPOLYLINE":
        vertices = entity.get("vertices")
        _require_vertices(vertices, path, min_vertices=2)
    elif kind == "TEXT":
        _require_point2(entity, "insert", path)
        _require_positive_number(entity, "height", path)
        if not isinstance(entity.get("text"), str):
            raise IRValidationError(f"{path}.text must be a string")
        if "halign" in entity and entity["halign"] not in {"left", "center", "right"}:
            raise IRValidationError(f"{path}.halign is invalid")
        if "valign" in entity and entity["valign"] not in {
            "baseline",
            "bottom",
            "middle",
            "top",
        }:
            raise IRValidationError(f"{path}.valign is invalid")
    elif kind == "MTEXT":
        _require_point2(entity, "insert", path)
        _require_positive_number(entity, "height", path)
        if not isinstance(entity.get("text"), str):
            raise IRValidationError(f"{path}.text must be a string")
        if "width" in entity:
            _require_non_negative_number(entity, "width", path)
        if "attach" in entity and entity["attach"] not in {
            "top_left",
            "top_center",
            "top_right",
            "middle_left",
            "middle_center",
            "middle_right",
            "bottom_left",
            "bottom_center",
            "bottom_right",
        }:
            raise IRValidationError(f"{path}.attach is invalid")
    elif kind == "INSERT":
        if not isinstance(entity.get("block"), str) or not entity["block"]:
            raise IRValidationError(f"{path}.block must be a non-empty string")
        _require_point2(entity, "insert", path)
        if "scale" in entity:
            scale = entity["scale"]
            if isinstance(scale, (int, float)):
                if float(scale) == 0:
                    raise IRValidationError(f"{path}.scale must be non-zero")
            elif (
                isinstance(scale, list)
                and len(scale) == 2
                and all(isinstance(v, (int, float)) for v in scale)
            ):
                if float(scale[0]) == 0 or float(scale[1]) == 0:
                    raise IRValidationError(f"{path}.scale values must be non-zero")
            else:
                raise IRValidationError(f"{path}.scale must be number or [sx,sy]")
        if "transform" in entity:
            transform = entity["transform"]
            if not isinstance(transform, list) or len(transform) != 6:
                raise IRValidationError(f"{path}.transform must be [a,b,c,d,e,f]")
            if not all(isinstance(value, (int, float)) for value in transform):
                raise IRValidationError(f"{path}.transform must contain only numbers")
        if "attributes" in entity:
            attributes = entity["attributes"]
            if not isinstance(attributes, dict):
                raise IRValidationError(f"{path}.attributes must be an object")
            for key, value in attributes.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise IRValidationError(
                        f"{path}.attributes must map string to string"
                    )
    elif kind == "HATCH":
        loops = entity.get("loops")
        if not isinstance(loops, list) or len(loops) < 1:
            raise IRValidationError(f"{path}.loops must contain at least one loop")
        for loop_idx, loop in enumerate(loops):
            if not isinstance(loop, dict):
                raise IRValidationError(f"{path}.loops[{loop_idx}] must be an object")
            vertices = loop.get("vertices")
            _require_vertices(vertices, f"{path}.loops[{loop_idx}]", min_vertices=3)
            if "is_outer" in loop and not isinstance(loop["is_outer"], bool):
                raise IRValidationError(
                    f"{path}.loops[{loop_idx}].is_outer must be boolean"
                )
        if "solid" in entity and not isinstance(entity["solid"], bool):
            raise IRValidationError(f"{path}.solid must be boolean")
        if "pattern" in entity and not isinstance(entity["pattern"], str):
            raise IRValidationError(f"{path}.pattern must be string")
    elif kind == "SPLINE":
        degree = entity.get("degree")
        if not isinstance(degree, int) or not (1 <= degree <= 7):
            raise IRValidationError(f"{path}.degree must be integer in [1, 7]")
        control_points = entity.get("control_points")
        if not isinstance(control_points, list) or len(control_points) < 2:
            raise IRValidationError(
                f"{path}.control_points must contain at least 2 points"
            )
        for cp in control_points:
            if (
                not isinstance(cp, list)
                or len(cp) != 2
                or not all(isinstance(v, (int, float)) for v in cp)
            ):
                raise IRValidationError(
                    f"{path}.control_points must contain [x, y] points"
                )

        if "knots" in entity:
            knots = entity["knots"]
            if not isinstance(knots, list) or not knots:
                raise IRValidationError(f"{path}.knots must be a non-empty array")
            if not all(isinstance(v, (int, float)) for v in knots):
                raise IRValidationError(f"{path}.knots must contain only numbers")

        if "weights" in entity:
            weights = entity["weights"]
            if not isinstance(weights, list) or not weights:
                raise IRValidationError(f"{path}.weights must be a non-empty array")
            if not all(isinstance(v, (int, float)) and float(v) > 0 for v in weights):
                raise IRValidationError(
                    f"{path}.weights must contain only positive numbers"
                )
            if len(weights) != len(control_points):
                raise IRValidationError(
                    f"{path}.weights length must match control_points length"
                )

        if "closed" in entity and not isinstance(entity["closed"], bool):
            raise IRValidationError(f"{path}.closed must be boolean")
    elif kind == "DIMENSION":
        if entity.get("dim_kind") not in {
            "GENERIC",
            "LINEAR",
            "ALIGNED",
            "ANGULAR",
            "RADIAL",
            "DIAMETER",
            "ORDINATE",
        }:
            raise IRValidationError(f"{path}.dim_kind is invalid")
        if "style" in entity and not isinstance(entity["style"], str):
            raise IRValidationError(f"{path}.style must be string")
        definition = entity.get("definition")
        if not isinstance(definition, dict):
            raise IRValidationError(f"{path}.definition must be an object")

    _validate_source(entity.get("source"), f"{path}.source")
    approximation = entity.get("approximation")
    if approximation is not None:
        if not isinstance(approximation, dict):
            raise IRValidationError(f"{path}.approximation must be an object")
        if approximation.get("method") not in {"polyline", "spline", "other"}:
            raise IRValidationError(f"{path}.approximation.method is invalid")
        if (
            not isinstance(approximation.get("source_kind"), str)
            or not approximation["source_kind"]
        ):
            raise IRValidationError(
                f"{path}.approximation.source_kind must be a non-empty string"
            )
        if "segments" in approximation:
            segments = approximation["segments"]
            if not isinstance(segments, int) or segments < 1:
                raise IRValidationError(f"{path}.approximation.segments must be >= 1")


def _validate_source(source: Any, path: str) -> None:
    if source is None:
        return
    if not isinstance(source, dict):
        raise IRValidationError(f"{path} must be an object")
    if source.get("format") not in {"dxf", "dwg", "jww", "sxf", "unknown"}:
        raise IRValidationError(f"{path}.format is invalid")
    for key in ("version", "name", "sha256", "id", "kind"):
        if key in source and not isinstance(source[key], str):
            raise IRValidationError(f"{path}.{key} must be a string")


def _validate_constraints(constraints: Any) -> None:
    if constraints is None:
        return
    if not isinstance(constraints, list):
        raise IRValidationError("constraints must be an array")

    valid_kinds = {
        "COINCIDENT",
        "HORIZONTAL",
        "VERTICAL",
        "PARALLEL",
        "PERPENDICULAR",
        "CONCENTRIC",
        "EQUAL_LENGTH",
        "EQUAL_RADIUS",
        "COLLINEAR",
    }
    for idx, constraint in enumerate(constraints):
        path = f"constraints[{idx}]"
        if not isinstance(constraint, dict):
            raise IRValidationError(f"{path} must be an object")
        kind = constraint.get("kind")
        if kind not in valid_kinds:
            raise IRValidationError(f"{path}.kind is invalid")
        refs = constraint.get("refs")
        if not isinstance(refs, list) or len(refs) < 1:
            raise IRValidationError(f"{path}.refs must contain at least one reference")
        for ref_idx, ref in enumerate(refs):
            ref_path = f"{path}.refs[{ref_idx}]"
            if not isinstance(ref, dict):
                raise IRValidationError(f"{ref_path} must be an object")
            if not isinstance(ref.get("entity"), str) or not ref["entity"]:
                raise IRValidationError(f"{ref_path}.entity must be a non-empty string")
            if not isinstance(ref.get("feature"), str) or not ref["feature"]:
                raise IRValidationError(
                    f"{ref_path}.feature must be a non-empty string"
                )

        if "tolerance" in constraint:
            tolerance = constraint["tolerance"]
            if not isinstance(tolerance, (int, float)) or float(tolerance) < 0:
                raise IRValidationError(f"{path}.tolerance must be >= 0")


def _require_vertices(vertices: Any, path: str, *, min_vertices: int) -> None:
    if not isinstance(vertices, list) or len(vertices) < min_vertices:
        raise IRValidationError(
            f"{path}.vertices must contain at least {min_vertices} vertices"
        )
    for vertex in vertices:
        if not isinstance(vertex, list) or len(vertex) not in {2, 3}:
            raise IRValidationError(f"{path}.vertices must be [x,y] or [x,y,bulge]")
        if not all(isinstance(v, (int, float)) for v in vertex):
            raise IRValidationError(f"{path}.vertices must contain only numbers")


def _require_point2(entity: dict[str, Any], key: str, path: str) -> None:
    value = entity.get(key)
    if not isinstance(value, list) or len(value) != 2:
        raise IRValidationError(f"{path}.{key} must be [x, y]")
    if not all(isinstance(v, (int, float)) for v in value):
        raise IRValidationError(f"{path}.{key} must contain only numbers")


def _require_number(entity: dict[str, Any], key: str, path: str) -> None:
    value = entity.get(key)
    if not isinstance(value, (int, float)):
        raise IRValidationError(f"{path}.{key} must be a number")


def _require_non_negative_number(entity: dict[str, Any], key: str, path: str) -> None:
    _require_number(entity, key, path)
    if float(entity[key]) < 0:
        raise IRValidationError(f"{path}.{key} must be >= 0")


def _require_positive_number(entity: dict[str, Any], key: str, path: str) -> None:
    _require_number(entity, key, path)
    if float(entity[key]) <= 0:
        raise IRValidationError(f"{path}.{key} must be > 0")
