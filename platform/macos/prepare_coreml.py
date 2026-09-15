"""Convert EDAP's YOLO models to Core ML packages for CPU + Neural Engine use."""

from __future__ import annotations

from pathlib import Path
import shutil

from ultralytics import YOLO


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".native-runtime/coreml"
MODELS = {
    "compass": ROOT / "Yolo26/compass-model/weights/best.pt",
    "target": ROOT / "Yolo26/target-model/weights/best.pt",
}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, source in MODELS.items():
        destination = OUTPUT / f"{name}.mlpackage"
        if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
            print(f"Core ML model is current: {destination}")
            continue
        exported = Path(YOLO(str(source)).export(format="coreml", imgsz=640, nms=False))
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(exported), destination)
        print(f"Prepared Core ML model: {destination}")


if __name__ == "__main__":
    main()
