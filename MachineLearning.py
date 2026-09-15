from __future__ import annotations

import enum
import os
import threading
from dataclasses import dataclass
import cv2
import torch
from ultralytics import YOLO
from Screen_Regions import Quad

"""
File:Machine_Learning.py

Description:
  Class for Machine Learning using Yolo26.
  Ref: https://docs.ultralytics.com/

Author: Stumpii
"""


@dataclass
class MachLearnMatch:
    """ A machine learning match. """
    class_name: str  # i.e. 'compass'
    match_pct: float  # i.e. 0.0 - 1.0
    bounding_quad: Quad  # The bounding box


class ModelType(enum.Enum):
    Compass = 0
    Target = 1


class MachLearn:
    def __init__(self, ed_ap, cb):
        self.ap = ed_ap
        self.ap_ckb = cb
        self._prediction_lock = threading.Lock()

        # The MoltenVR launcher limits OpenMP for PaddleOCR stability. Restore
        # a small PyTorch intra-op pool so YOLO does not inherit one thread.
        torch_threads = max(1, int(os.environ.get("EDAP_TORCH_THREADS", "4")))
        torch.set_num_threads(torch_threads)
        requested_device = os.environ.get("EDAP_ML_DEVICE", "cpu").lower()
        self.coreml_models = {}
        if requested_device == "ane":
            self._load_coreml_models()
        if requested_device == "mps" and torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
        compute = "Core ML CPU + Neural Engine (GPU excluded)" if self.coreml_models else self.device
        self.ap_ckb('log', f"YOLO compute: {compute}; PyTorch threads: {torch.get_num_threads()}")

        self.compass_ml_model = YOLO("Yolo26/compass-model/weights/best.pt")
        self.target_ml_model = YOLO("Yolo26/target-model/weights/best.pt")

    def _load_coreml_models(self):
        """Load converted models while explicitly excluding the GPU."""
        try:
            import coremltools as ct
            root = os.path.join(".native-runtime", "coreml")
            paths = {
                ModelType.Compass: os.path.join(root, "compass.mlpackage"),
                ModelType.Target: os.path.join(root, "target.mlpackage"),
            }
            if not all(os.path.isdir(path) for path in paths.values()):
                self.ap_ckb('log', "Core ML models not prepared; using PyTorch CPU")
                return
            self.coreml_models = {
                kind: ct.models.MLModel(path, compute_units=ct.ComputeUnit.CPU_AND_NE)
                for kind, path in paths.items()
            }
        except Exception as exc:
            self.coreml_models = {}
            self.ap_ckb('log', f"Core ML unavailable ({exc}); using PyTorch CPU")

    def _coreml_predict(self, model: ModelType, image, class_name: str):
        from PIL import Image

        if image is None or getattr(image, "size", 0) == 0:
            return None
        if image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        height, width = image.shape[:2]
        gain = min(640.0 / width, 640.0 / height)
        scaled_width, scaled_height = round(width * gain), round(height * gain)
        resized = cv2.resize(image, (scaled_width, scaled_height), interpolation=cv2.INTER_LINEAR)
        pad_x, pad_y = (640 - scaled_width) / 2.0, (640 - scaled_height) / 2.0
        left, top = round(pad_x - 0.1), round(pad_y - 0.1)
        right, bottom = round(pad_x + 0.1), round(pad_y + 0.1)
        letterboxed = cv2.copyMakeBorder(
            resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
        pil_image = Image.fromarray(cv2.cvtColor(letterboxed, cv2.COLOR_BGR2RGB))
        output = next(iter(self.coreml_models[model].predict({"image": pil_image}).values()))[0]
        names = (self.compass_ml_model.names if model is ModelType.Compass
                 else self.target_ml_model.names)
        matches = []
        for row in output:
            confidence = float(row[4])
            if confidence < 0.25:
                continue
            detected_name = names[int(row[5])]
            if class_name and detected_name != class_name:
                continue
            x1 = max(0.0, min(width, (float(row[0]) - left) / gain))
            y1 = max(0.0, min(height, (float(row[1]) - top) / gain))
            x2 = max(0.0, min(width, (float(row[2]) - left) / gain))
            y2 = max(0.0, min(height, (float(row[3]) - top) / gain))
            matches.append(MachLearnMatch(
                class_name=detected_name,
                match_pct=confidence,
                bounding_quad=Quad.from_rect([x1, y1, x2, y2])))
        return matches or None

    def model_predict(self, model: ModelType, image, class_name: str) -> list[MachLearnMatch] | None:
        with self._prediction_lock:
            return self._model_predict(model, image, class_name)

    def _model_predict(self, model: ModelType, image, class_name: str) -> list[MachLearnMatch] | None:
        """ Performs a prediction of an image using the relevant model and returns the results.
        @param model: Model type (i.e. Compass or Target)
        @param image: The image to check.
        @param class_name: The class name to filter by i.e.
         for Compass Model: 'compass', 'navpoint and 'navpoint-behind'.
         for Target Model: 'target', 'target-occluded'.
        @return: A list of learning matches.
        """
        results = None
        matches: list[MachLearnMatch] = []
        self.ap.raise_if_stop_requested()
        if model in self.coreml_models:
            try:
                matches = self._coreml_predict(model, image, class_name)
                self.ap.raise_if_stop_requested()
                return matches
            except Exception as exc:
                self.ap_ckb('log', f"Core ML inference failed ({exc}); switching to CPU")
                self.coreml_models = {}
        # Do prediction with ML
        if model is ModelType.Compass:
            results = self.compass_ml_model.predict(image, verbose=False, device=self.device)
        elif model is ModelType.Target:
            results = self.target_ml_model.predict(image, verbose=False, device=self.device)

        # Native inference cannot be cancelled safely, so reject its result as
        # soon as it returns if the user requested a stop meanwhile.
        self.ap.raise_if_stop_requested()

        if results and len(results) == 1:
            r = results[0]
            if len(r.boxes) > 0:
                for b in r.boxes:
                    clsid = int(b.cls.item())
                    name = r.names[clsid]  # Class name
                    # Is name wanted
                    if class_name == '' or name == class_name:
                        confidence = b.conf.item()  # Confidence %
                        rect_tmp = b.xyxy.tolist()  # Match as a rect
                        rect_tmp = rect_tmp[0]
                        res_quad = Quad.from_rect(rect_tmp)

                        # Add item
                        match = MachLearnMatch(class_name=name, match_pct=confidence, bounding_quad=res_quad)
                        matches.append(match)
                return matches
            else:
                return None
