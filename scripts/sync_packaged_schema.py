from __future__ import annotations

from pathlib import Path
import shutil


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "ir_schema.json"
    target = root / "src" / "cad2d_ir" / "data" / "ir_schema.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"synced: {source} -> {target}")


if __name__ == "__main__":
    main()
