"""
Utility: Custom "Hey Jarvis" wake word model

Run this standalone - it is not imported by the assistant:

    python train_jarvis.py            # enable the official "Hey Jarvis" model (default)
    python train_jarvis.py --check    # preflight for training a model from scratch
    python train_jarvis.py --scaffold # write a starter training config

-------------------------------------------------------------------
READ THIS FIRST - you probably do not need to train anything
-------------------------------------------------------------------
openWakeWord already ships an official, pretrained "hey_jarvis" model.
It is a first-class model in openwakeword.MODELS, exactly like "alexa"
and "hey_mycroft" - it is simply not downloaded by a bare
`pip install openwakeword`, which is what produces the confusing
"NO_SUCHFILE ... hey_jarvis_v0.1.onnx" error.

So the fast path to a working "Hey Jarvis" is a download, not a
training run. That is what the default mode of this script does, and
it takes about ten seconds.

Train your own model only if you want a *different* phrase, or a model
tuned to your specific voice and room. That is a genuinely heavy job -
see --check for exactly what it requires.
"""

from __future__ import annotations

import argparse
import os
import sys

MODEL_KEY = "hey_jarvis"

# Packages the real training pipeline needs on top of the runtime deps,
# as (import name, pip name, why). The import name and the pip name
# differ for yaml/pyyaml, so both are tracked.
_TRAINING_REQUIREMENTS = [
    ("torch", "torch", "PyTorch - the training framework (GPU strongly recommended)"),
    ("torchinfo", "torchinfo", "Model summary utilities used by openwakeword.train"),
    ("torchmetrics", "torchmetrics", "Metric tracking during training"),
    ("yaml", "pyyaml", "Reads the training config"),
    ("scipy", "scipy", "Signal processing for audio augmentation"),
    ("tqdm", "tqdm", "Progress bars"),
]

_STARTER_CONFIG = """\
# Starter openWakeWord training config for a custom wake word.
# Fill in every path marked TODO before running the trainer.
#
# Run with:
#   python -m openwakeword.train --training_config my_jarvis.yaml \\
#       --generate_clips --augment_clips --train_model

model_name: hey_jarvis_custom
target_phrase:
  - "hey jarvis"

# How many synthetic clips to synthesise. More = better, and slower.
n_samples: 30000
n_samples_val: 2000

# TODO: clone https://github.com/rhasspy/piper-sample-generator
# and point this at that folder (it provides generate_samples.py).
piper_sample_generator_path: "./piper-sample-generator"

# TODO: room impulse response WAVs (e.g. the MIT RIR survey).
rir_paths:
  - "./data/rirs"

# TODO: background/noise audio (e.g. FMA, AudioSet, ACAV100M excerpts).
# This is what teaches the model NOT to fire at everything.
background_paths:
  - "./data/background"
background_paths_duplication_rate:
  - 1

# TODO: precomputed openWakeWord negative features (.npy).
false_positive_validation_data_path: "./data/validation_set_features.npy"
feature_data_files: {}

output_dir: "./jarvis_training_output"

batch_size: 1024
steps: 50000
max_negative_weight: 1500
target_accuracy: 0.7
target_recall: 0.5
"""


def _models_dir() -> str:
    """Filesystem folder where openWakeWord keeps its model files."""
    import openwakeword

    return os.path.join(os.path.dirname(os.path.abspath(openwakeword.__file__)), "resources", "models")


def enable_pretrained() -> int:
    """
    Downloads (if needed) and verifies the official pretrained
    "hey_jarvis" model, then prints exactly where it lives and how to
    switch the assistant over to it.
    """
    try:
        import openwakeword
        import openwakeword.utils
    except ImportError:
        print("[Train] openwakeword is not installed. Run: pip install openwakeword")
        return 1

    if MODEL_KEY not in openwakeword.MODELS:
        print(f"[Train] '{MODEL_KEY}' is not a known pretrained model in this openwakeword version.")
        print(f"[Train] Available: {', '.join(openwakeword.MODELS)}")
        return 1

    tflite_path = openwakeword.MODELS[MODEL_KEY]["model_path"]
    onnx_path = tflite_path.replace(".tflite", ".onnx")

    if os.path.exists(onnx_path) and os.path.exists(tflite_path):
        print(f"[Train] '{MODEL_KEY}' is already downloaded - nothing to do.")
    else:
        print(f"[Train] Downloading the pretrained '{MODEL_KEY}' model (one time, needs internet)...")
        try:
            openwakeword.utils.download_models(model_names=[MODEL_KEY])
        except Exception as exc:  # noqa: BLE001 - network/IO failures must read clearly
            print(f"[Train] Download failed: {exc}")
            print("[Train] Check your internet connection and try again.")
            return 1

    missing = [p for p in (onnx_path, tflite_path) if not os.path.exists(p)]
    if missing:
        print("[Train] Download finished but these files are still missing:")
        for path in missing:
            print(f"          {path}")
        return 1

    # Prove the model actually loads under the runtime's ONNX backend.
    try:
        from openwakeword.model import Model

        Model(wakeword_models=[MODEL_KEY], inference_framework="onnx")
        loaded = True
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        print(f"[Train] Model files exist but failed to load: {exc}")
        loaded = False

    size_kb = os.path.getsize(onnx_path) / 1024
    print()
    print("=" * 68)
    print("  'HEY JARVIS' IS READY")
    print("=" * 68)
    print(f"  ONNX model : {onnx_path}")
    print(f"  Size       : {size_kb:,.0f} KB")
    print(f"  Loads OK   : {'yes' if loaded else 'NO - see error above'}")
    print()
    print("  These files are already inside the openwakeword package, so")
    print("  there is NOTHING TO MOVE. The engine finds them by name.")
    print()
    print("  To activate, set this in wake_word_engine.py:")
    print()
    print('      WAKE_WORD_MODEL = "hey_jarvis"')
    print()
    print("  Then run:  .venv\\Scripts\\python main.py")
    print("=" * 68)
    return 0 if loaded else 1


def check_training_prereqs() -> int:
    """Reports what a genuine from-scratch training run still needs."""
    print("=" * 68)
    print("  PREFLIGHT: training a custom wake word from scratch")
    print("=" * 68)
    print()
    print("Python packages:")
    missing_pkgs = []
    for module, pip_name, why in _TRAINING_REQUIREMENTS:
        try:
            __import__(module)
            print(f"  [ok]      {pip_name:<14} {why}")
        except ImportError:
            missing_pkgs.append(pip_name)
            print(f"  [MISSING] {pip_name:<14} {why}")

    print()
    print("External data and tooling (not pip-installable):")
    piper = os.path.isdir("./piper-sample-generator")
    print(f"  [{'ok' if piper else 'MISSING'}] piper-sample-generator")
    print("           git clone https://github.com/rhasspy/piper-sample-generator")
    print("           + its ~1 GB TTS checkpoint. Generates the positive samples.")
    print("  [ ? ] Room impulse responses (e.g. MIT RIR survey), ~1 GB")
    print("  [ ? ] Background/noise audio (FMA, AudioSet, ACAV100M), 10-100+ GB")
    print("  [ ? ] Precomputed negative validation features (.npy)")

    print()
    print("Hardware:")
    try:
        import torch

        cuda = torch.cuda.is_available()
        print(f"  CUDA GPU available: {'yes - ' + torch.cuda.get_device_name(0) if cuda else 'no'}")
        if not cuda:
            print("           CPU-only training is impractical (days, not hours).")
    except ImportError:
        print("  CUDA GPU available: unknown (torch not installed)")

    print()
    print("-" * 68)
    print("HONEST SUMMARY")
    print("-" * 68)
    print("  Training a wake word from scratch is a multi-hour GPU job over")
    print("  tens of gigabytes of audio. It is not a run-once local script.")
    print()
    print("  Recommended routes, in order:")
    print("   1. Use the official pretrained model:  python train_jarvis.py")
    print("      (You almost certainly want this. It is already on disk.)")
    print("   2. Use the maintainer's Colab notebook for a custom phrase -")
    print("      free GPU, datasets pre-wired:")
    print("      https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb")
    print("   3. Only then, local training via --scaffold + openwakeword.train")
    if missing_pkgs:
        print()
        print(f"  To attempt route 3 locally, first: pip install {' '.join(missing_pkgs)}")
    print("-" * 68)
    return 0


def scaffold_config(path: str = "my_jarvis.yaml") -> int:
    """Writes a starter training config with every required field marked."""
    if os.path.exists(path):
        print(f"[Train] {path} already exists - not overwriting it.")
        return 1
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_STARTER_CONFIG)
    except OSError as exc:
        print(f"[Train] Could not write {path}: {exc}")
        return 1

    print(f"[Train] Wrote starter training config: {os.path.abspath(path)}")
    print("[Train] Every 'TODO' line must be filled in before training will run.")
    print("[Train] Then: python -m openwakeword.train --training_config "
          f"{path} --generate_clips --augment_clips --train_model")
    print()
    print("[Train] When training finishes, the model lands in:")
    print("[Train]     ./jarvis_training_output/hey_jarvis_custom/*.onnx")
    print("[Train] Copy that .onnx next to this project, then point")
    print("[Train] wake_word_engine.py at it directly:")
    print()
    print('[Train]     WAKE_WORD_MODEL = "./hey_jarvis_custom.onnx"')
    print()
    print("[Train] (WAKE_WORD_MODEL accepts a filesystem path as well as a")
    print("[Train]  built-in model name, so no file needs to go into site-packages.)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up the 'Hey Jarvis' wake word model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="Report what from-scratch training would require.",
    )
    group.add_argument(
        "--scaffold",
        action="store_true",
        help="Write a starter training config (my_jarvis.yaml).",
    )
    args = parser.parse_args()

    if args.check:
        return check_training_prereqs()
    if args.scaffold:
        return scaffold_config()
    return enable_pretrained()


if __name__ == "__main__":
    sys.exit(main())
