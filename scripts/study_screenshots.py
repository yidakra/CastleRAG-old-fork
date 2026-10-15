"""Capture user-study screenshots and a manifest from the live CastleRAG UI.

Runs on a laptop against the dashboard that ``scripts/slurm/ui_live.slurm``
serves on a Snellius compute node. That job binds ``castlerag ui`` to port
**8050** (``UI_PORT``; the CLI's own default of 50225 does not apply) and prints
the node name in ``logs/castle-ui-live_<jobid>.out``. Forward it with:

    ssh -L 8050:<NODE>:8050 <user>@snellius.surf.nl

then run (default ``--url`` is http://localhost:8050/):

    pip install -e ".[study]" && python -m playwright install chromium
    python scripts/study_screenshots.py                  # all 9 candidate tasks
    python scripts/study_screenshots.py --tasks 1,3-4 --annotate

If the job runs with ``PUBLIC_TUNNEL=1`` the app enforces basic auth; export
``CASTLERAG_UI_BASIC_AUTH=user:pass`` and the script sends it.

Per task it asks the question, then saves to ``outputs/user_study/taskNN_<slug>/``:

    01_answer.png    the answer card (claim + support badge)
    02_evidence.png  top evidence moment selected: synchronized cameras + scores
    03_review.png    per-camera verdicts applied, drafted refined query visible
    04_refined.png   result of the refined search
    00_orientation.png  (orientation task only) numbered callouts with --annotate
    thread.json      the UI's own "Export" of the thread

and writes ``outputs/user_study/manifest.json`` (question, ground truth from
``data/smoke_day1.csv``, answer, claim, support, cited moments, a heuristic
``answer_mentions_truth`` flagged for manual check, and seeked YouTube
``watch_url`` per displayed camera).

Verdicts default to Confirm on the top-scoring camera and Refine on the others;
override per task with ``--plan plan.json``::

    {"1": {"best": "confirm", "others": "ignore"}, "3": {"Luca": "refine"}}

The script refuses to run unless the top bar shows the "live RAG" badge:
offline answers are fabricated and must never reach the study. ``--allow-offline``
exists only to dry-run the flow against a local ``castlerag ui``.

Browser-free logic lives in ``castlerag.study.capture`` (unit-tested).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from castlerag.study import capture as cap
from castlerag.ui.youtube import YouTubeMirror

VIEWPORT = {"width": 1600, "height": 1000}
SCALE = 2  # device-scale factor: crisp screenshots for the paper/slides


class OfflineError(RuntimeError):
    """The UI dropped out of live mode mid-run (e.g. the job restarted)."""


def log(msg: str) -> None:
    print(f"[study] {msg}", flush=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=cap.DEFAULT_UI_URL,
                    help="dashboard URL (default: %(default)s, the ui_live.slurm port)")
    ap.add_argument("--questions", default=None,
                    help="file with one question per line, or '|'-separated "
                         "questions (default: the 9 study candidates)")
    ap.add_argument("--tasks", default="all",
                    help="task numbers to run, e.g. '1,3-5' (default: all)")
    ap.add_argument("--orientation", type=int, default=1,
                    help="task number that also gets 00_orientation.png "
                         "(0 disables; default: %(default)s)")
    ap.add_argument("--annotate", action="store_true",
                    help="draw numbered callouts on 00_orientation.png")
    ap.add_argument("--plan", type=Path, default=None,
                    help="JSON verdict plan keyed by task number")
    ap.add_argument("--csv", type=Path, default=cap.DEFAULT_CSV,
                    help="ground-truth CSV (default: %(default)s)")
    ap.add_argument("--out", type=Path, default=cap.DEFAULT_OUT_DIR,
                    help="output root (default: %(default)s)")
    ap.add_argument("--answer-timeout", type=float, default=600.0,
                    help="seconds to wait for an answer/refine (default: %(default)s)")
    ap.add_argument("--step-timeout", type=float, default=180.0,
                    help="seconds to wait for a verdict draft / submit "
                         "(default: %(default)s)")
    ap.add_argument("--allow-offline", action="store_true",
                    help="proceed even if the UI is in offline (fabricated) mode")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------


def wait_js(page: Any, expr: str, timeout_s: float, arg: Any = None) -> None:
    page.wait_for_function(expr, arg=arg, timeout=timeout_s * 1000, polling=500)


def settle(page: Any, seconds: float = 1.5) -> None:
    """Let Dash finish re-rendering (spinners, Plotly) before a screenshot."""
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # long-poll free app; networkidle should come quickly
        pass
    time.sleep(seconds)


def card_count(page: Any) -> int:
    return int(page.locator(cap.SEL_GROUP_CARD).count())


def wait_new_card(page: Any, before: int, timeout_s: float) -> None:
    wait_js(
        page,
        "([sel, n]) => document.querySelectorAll(sel).length > n",
        timeout_s,
        [cap.SEL_GROUP_CARD, before],
    )
    page.locator(cap.SEL_CAMERA_TILE).first.wait_for(
        state="visible", timeout=timeout_s * 1000
    )


def unhidden(page: Any, element_id: str, timeout_s: float) -> None:
    wait_js(
        page,
        "id => { const el = document.getElementById(id); return el && !el.hidden; }",
        timeout_s,
        element_id,
    )


def is_unhidden(page: Any, element_id: str) -> bool:
    return bool(page.evaluate(
        "id => { const el = document.getElementById(id); return !!el && !el.hidden; }",
        element_id,
    ))


def export_thread(page: Any) -> Dict[str, Any]:
    """Click the top-bar Export button and parse the downloaded thread JSON."""
    with page.expect_download(timeout=30_000) as dl:
        page.locator(cap.SEL_EXPORT_BUTTON).click()
    path = dl.value.path()
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_mode(page: Any) -> str:
    badge = page.locator(cap.SEL_MODE_BADGE)
    badge.wait_for(state="visible", timeout=30_000)
    return "live" if cap.is_live_badge(badge.inner_text()) else "offline"


def shoot(page: Any, path: Path) -> str:
    page.screenshot(path=str(path))
    log(f"  saved {path}")
    return path.name


def fit_viewer(page: Any) -> None:
    """Grow the viewport so the whole evidence viewer (cameras, scores, review
    row, drafted refined query) fits without its internal scroll."""
    needed = page.evaluate(
        "() => { const v = document.querySelector('.viewer-col');"
        " if (!v) return 0; const r = v.getBoundingClientRect();"
        " return Math.ceil(r.top + v.scrollHeight + 40); }"
    )
    height = max(VIEWPORT["height"], int(needed or 0))
    page.set_viewport_size({"width": VIEWPORT["width"], "height": height})
    time.sleep(0.8)  # let the flex layout and Plotly re-measure


def reset_viewport(page: Any) -> None:
    page.set_viewport_size(VIEWPORT)
    time.sleep(0.5)


def orientation_boxes(page: Any) -> List[tuple]:
    """Bounding boxes (CSS px, viewport-relative) of the orientation targets."""
    targets = []
    for number, label, selector in cap.ORIENTATION_TARGETS:
        loc = page.locator(selector)
        loc = loc.last if selector == cap.SEL_GROUP_CARD else loc.first
        box = loc.bounding_box() if loc.count() else None
        targets.append(
            (number, label,
             (box["x"], box["y"], box["width"], box["height"]) if box else None)
        )
    return targets


# ---------------------------------------------------------------------------
# One task
# ---------------------------------------------------------------------------


def run_task(
    page: Any,
    task: cap.StudyTask,
    args: argparse.Namespace,
    plan: Dict[int, Dict[str, str]],
    mirror: YouTubeMirror,
) -> Dict[str, Any]:
    task_dir = args.out / task.dir_name
    task_dir.mkdir(parents=True, exist_ok=True)
    shots: List[str] = []
    log(f"task {task.number}: {task.question!r}")

    page.goto(args.url, wait_until="networkidle", timeout=60_000)
    mode = read_mode(page)
    if mode != "live" and not args.allow_offline:
        raise OfflineError(offline_refusal(args.url))

    # 1. ask, wait for the answer card
    before = card_count(page)
    page.locator(cap.SEL_QUESTION_INPUT).fill(task.question)
    page.locator(cap.SEL_ASK_BUTTON).click()
    wait_new_card(page, before, args.answer_timeout)
    settle(page)
    page.locator(cap.SEL_GROUP_CARD).last.scroll_into_view_if_needed()
    shots.append(shoot(page, task_dir / "01_answer.png"))

    # 2. select the top evidence moment of the new card
    page.locator(cap.SEL_GROUP_CARD).last.locator(cap.SEL_MOMENT_BUTTON).first.click()
    page.locator(cap.SEL_CAMERA_TILE).first.wait_for(state="visible")
    settle(page, 2.5)  # iframes + Plotly score chart
    shots.append(shoot(page, task_dir / "02_evidence.png"))

    # 3. verdicts on the displayed cameras
    export = export_thread(page)
    initial, _ = cap.split_turns(export)
    top = ((initial or {}).get("moments") or [{}])[0]
    cameras = top.get("cameras") or []
    verdicts = cap.resolve_verdicts(cameras, plan.get(task.number))
    log(f"  verdicts: {verdicts}")
    for cam, action in verdicts.items():
        page.locator(cap.review_button_selector(cam, action)).click()
        # The justification draft appears once the verdict callback returns
        # (the live engine drafts it with the LLM) — wait before the next click.
        page.locator(cap.review_just_selector(cam)).wait_for(
            state="visible", timeout=args.step_timeout * 1000
        )
    unhidden(page, "submit-wrap", args.step_timeout)
    page.locator(cap.SEL_SUBMIT_REVIEWS).click()
    wait_js(
        page,
        "() => { const c = document.getElementById('compose-wrap');"
        " const b = document.getElementById('converged-banner');"
        " const q = document.getElementById('refined-query-input');"
        " return (c && !c.hidden && q && q.value.trim().length > 0)"
        " || (b && !b.hidden); }",
        args.step_timeout,
    )
    converged = is_unhidden(page, "converged-banner")
    settle(page)
    fit_viewer(page)  # review row + drafted refined query both on screen
    shots.append(shoot(page, task_dir / "03_review.png"))

    if task.number == args.orientation:
        page.locator(cap.SEL_GROUP_CARD).last.scroll_into_view_if_needed()
        raw = task_dir / "00_orientation.png"
        shots.insert(0, shoot(page, raw))
        boxes = orientation_boxes(page)
        (task_dir / "00_orientation_boxes.json").write_text(
            json.dumps({"scale": SCALE, "targets": boxes}, indent=2)
        )
        if args.annotate:
            from PIL import Image

            with Image.open(raw) as im:
                size = im.size
            cap.annotate_image(raw, raw, cap.callout_layout(boxes, size, scale=SCALE))
            log("  annotated orientation callouts")
    reset_viewport(page)

    # 4. send the refined search
    if converged:
        log("  all verdicts confirm/ignore: search converged, no refine step")
    else:
        before = card_count(page)
        page.locator(cap.SEL_SEND_REFINED).click()
        wait_new_card(page, before, args.answer_timeout)
        settle(page, 2.5)
        page.locator(cap.SEL_GROUP_CARD).last.scroll_into_view_if_needed()
        shots.append(shoot(page, task_dir / "04_refined.png"))

    export = export_thread(page)
    (task_dir / "thread.json").write_text(
        json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return cap.build_task_entry(
        task, export, mirror,
        mode=mode, task_dir=task.dir_name, screenshots=shots,
        verdicts=verdicts, converged=converged,
    )


def offline_refusal(url: str) -> str:
    bar = "!" * 72
    return (
        f"\n{bar}\n"
        f"  {url} is NOT in live mode (no 'live RAG' badge in the top bar).\n"
        "  Offline answers are FABRICATED placeholders and must never be used\n"
        "  in the user study. Check the ui_live.slurm job and the SSH tunnel.\n"
        "  (Pass --allow-offline only to dry-run the capture flow.)\n"
        f"{bar}\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    tasks = cap.match_questions(
        cap.load_questions_arg(args.questions), cap.load_ground_truth(args.csv)
    )
    selected = set(cap.parse_task_selection(args.tasks, len(tasks)))
    plan = cap.load_verdict_plan(args.plan)
    mirror = YouTubeMirror.from_csv()
    args.out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.stderr.write(
            'playwright missing: pip install -e ".[study]" && '
            "python -m playwright install chromium\n"
        )
        return 2

    ctx_kwargs: Dict[str, Any] = {
        "viewport": VIEWPORT, "device_scale_factor": SCALE, "accept_downloads": True,
    }
    auth = os.environ.get("CASTLERAG_UI_BASIC_AUTH", "")
    if auth:
        user, _, password = auth.partition(":")
        ctx_kwargs["http_credentials"] = {"username": user, "password": password}

    entries: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        page.set_default_timeout(60_000)

        page.goto(args.url, wait_until="networkidle", timeout=60_000)
        mode = read_mode(page)
        if mode != "live":
            sys.stderr.write(offline_refusal(args.url))
            if not args.allow_offline:
                browser.close()
                return 3
            log("WARNING: --allow-offline set; answers below are FABRICATED")

        failed: List[int] = []
        went_offline = False
        for task in tasks:
            if task.number not in selected:
                continue
            try:
                entry = run_task(page, task, args, plan, mirror)
            except OfflineError as exc:
                # Stop, but still write the manifest for tasks already captured.
                sys.stderr.write(str(exc))
                went_offline = True
                break
            except Exception as exc:  # keep going; one slow task shouldn't sink the run
                failed.append(task.number)
                log(f"  ERROR task {task.number}: {exc}")
                err = args.out / task.dir_name / "error.png"
                try:
                    page.screenshot(path=str(err))
                except Exception:
                    pass
                continue
            entries.append(entry)
            log(f"  answer_mentions_truth={entry['answer_mentions_truth']} "
                f"(truth {task.truth!r}; check by hand)")
        browser.close()

    out = args.out / "manifest.json"
    previous: List[Dict[str, Any]] = []
    if out.exists():  # keep tasks captured by earlier runs that weren't re-run
        previous = json.loads(out.read_text(encoding="utf-8")).get("tasks", [])
    # Selected tasks are replaced even if they failed this time, so the manifest
    # never points at a stale earlier capture of a task we just re-ran.
    entries = cap.merge_entries(previous, entries, rerun=selected)
    modes = {e.get("mode") for e in entries}
    run_mode = "live" if modes == {"live"} else "offline"
    manifest = cap.build_manifest(
        entries,
        ui_url=args.url,
        mode=run_mode,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"wrote {out} ({len(entries)} tasks, mode={run_mode})")
    if went_offline:
        return 3
    if failed:
        log(f"FAILED tasks: {failed} (see error.png in their folders)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
