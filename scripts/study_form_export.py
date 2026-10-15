"""Copy the chosen tasks' screenshots under the Google Form's file names.

After a live capture with ``scripts/study_screenshots.py``, pick which task
fills each form slot (practice, t1 and t2 right with strong evidence, t3 right
but weakly supported, t4 wrong) and run::

    python scripts/study_form_export.py --slots practice=3,t1=8,t2=9,t3=7,t4=4

It copies ``orientation.png`` and ``<slot>_01_answer.png`` ...
``<slot>_04_refined.png`` into ``outputs/user_study/form/`` and writes
``form_constants.js`` with the ``TASKS`` and ``FOOTAGE`` constants for
``scripts/study_form.gs``. Upload the PNGs to one Drive folder, paste the
constants and the folder id into the script, and run ``buildForm``.

Warnings (offline capture, missing steps or links, an answer that doesn't fit
its slot) are printed; ``--strict`` turns them into exit status 1.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from castlerag.study import capture as cap


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--slots",
        required=True,
        help="practice=N,t1=N,t2=N,t3=N,t4=N (task numbers from the manifest)",
    )
    ap.add_argument(
        "--capture",
        type=Path,
        default=cap.DEFAULT_OUT_DIR,
        help="capture directory holding manifest.json (default: %(default)s)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: <capture>/form)",
    )
    ap.add_argument("--strict", action="store_true", help="exit 1 on any warning")
    args = ap.parse_args(argv)

    manifest = json.loads((args.capture / "manifest.json").read_text(encoding="utf-8"))
    plan = cap.form_export_plan(manifest, cap.parse_slots(args.slots))
    out = args.out or args.capture / "form"
    out.mkdir(parents=True, exist_ok=True)
    for src, dst in plan["copies"]:
        shutil.copy2(args.capture / src, out / dst)
    (out / "form_constants.js").write_text(
        cap.form_constants_js(plan), encoding="utf-8"
    )
    print(
        f"copied {len(plan['copies'])} screenshots to {out}; "
        f"constants in {out / 'form_constants.js'}"
    )
    for w in plan["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    return 1 if args.strict and plan["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
