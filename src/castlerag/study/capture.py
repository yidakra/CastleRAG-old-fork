"""Browser-free logic behind ``scripts/study_screenshots.py``.

The capture script drives the live dashboard with Playwright; everything that
does not need a browser lives here so it can be unit-tested: matching study
questions to the ground-truth CSV, picking per-camera verdicts, building Dash
selectors, turning the UI's exported thread into a manifest entry (with seeked
YouTube links from :class:`castlerag.ui.youtube.YouTubeMirror`), a rough
answer-vs-truth check, and the geometry/drawing of the orientation callouts.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from castlerag.ui.chat import CITE_MARKER_RE
from castlerag.ui.youtube import YouTubeMirror

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# scripts/slurm/ui_live.slurm serves on UI_PORT=8050 (not the CLI's 50225
# default), and the laptop forwards that same port over SSH.
DEFAULT_UI_URL = "http://localhost:8050/"
DEFAULT_CSV = Path("data/smoke_day1.csv")
DEFAULT_OUT_DIR = Path("outputs/user_study")

CANDIDATE_QUESTIONS: Tuple[str, ...] = (
    "What brand is the fridge in the kitchen?",
    "How many oranges were used to make the mulled wine?",
    "What organisation's logo is on Werner's apron?",
    "Who gave the first presentation for the workshop on the first day?",
    "When did Cathal want rösti?",
    "What was for lunch on the first day?",
    "Who won the first round of UNO?",
    "What game did Bjorn suggest to play with the cameras?",
    "Who brought the coffee pot to the table for breakfast on the first day?",
)

# ---------------------------------------------------------------------------
# Selectors — every one comes from src/castlerag/ui/layout.py or callbacks.py.
# tests/unit/test_study_capture.py checks the plain ids against build_layout().
# ---------------------------------------------------------------------------

# Dataset footage must not leave the team (CASTLE terms of use: no
# distribution of the data or derivatives), so study screenshots blur every
# embedded video and participants watch it through the public YouTube links.
SEL_FOOTAGE = "#camera-grid iframe.camera-frame"
FOOTAGE_MASK_CSS = (
    "#camera-grid iframe.camera-frame {"
    " filter: blur(28px) brightness(0.85) !important; pointer-events: none; }"
    " #camera-grid .camera-tile .mantine-AspectRatio-root { position: relative; }"
    " #camera-grid .camera-tile .mantine-AspectRatio-root::after {"
    " content: 'Footage via link'; position: absolute; inset: 0; display: flex;"
    " align-items: center; justify-content: center; color: #fff;"
    " font: 600 13px system-ui, sans-serif; text-shadow: 0 1px 3px #000; }"
)
FOOTAGE_MASK_INIT_JS = (
    "(() => { const css = %s; const add = () => {"
    " if (document.getElementById('study-footage-mask')) return;"
    " const s = document.createElement('style'); s.id = 'study-footage-mask';"
    " s.textContent = css; document.head.appendChild(s); };"
    " if (document.head) add();"
    " else document.addEventListener('DOMContentLoaded', add);"
    " })();" % json.dumps(FOOTAGE_MASK_CSS)
)
# Returns the number of visible footage frames that are NOT blurred.
FOOTAGE_UNMASKED_JS = (
    "() => Array.from(document.querySelectorAll(%s)).filter(f => {"
    " const r = f.getBoundingClientRect(); if (!r.width || !r.height) return false;"
    " return !getComputedStyle(f).filter.includes('blur'); }).length"
    % json.dumps(SEL_FOOTAGE)
)

SEL_MODE_BADGE = "#mode-badge"  # top-bar "live RAG" / "offline" badge
SEL_QUESTION_INPUT = "#new-question-input"
SEL_ASK_BUTTON = "#ask-new-button"
SEL_THREAD = "#thread"
SEL_GROUP_CARD = "#thread .group-card"  # one card per query group
SEL_MOMENT_BUTTON = "button.moment"  # ranked evidence-moment rows in a card
SEL_CAMERA_GRID = "#camera-grid"
SEL_CAMERA_TILE = "#camera-grid .camera-tile"
SEL_EVIDENCE_FIGURE = "#evidence-figure"  # per-camera match-score chart
SEL_REVIEW_ROW = "#review-row"
SEL_SUBMIT_WRAP = "#submit-wrap"
SEL_SUBMIT_REVIEWS = "#submit-reviews-button"
SEL_COMPOSE_WRAP = "#compose-wrap"
SEL_REFINED_INPUT = "#refined-query-input"
SEL_SEND_REFINED = "#send-refined-button"
SEL_CONVERGED_BANNER = "#converged-banner"
SEL_PENDING_CARD = "#pending-card"
SEL_EXPORT_BUTTON = "#export-thread-button"

# Plain element ids the selectors above rely on (layout-level, always present).
LAYOUT_IDS: Tuple[str, ...] = (
    "mode-badge",
    "new-question-input",
    "ask-new-button",
    "thread",
    "camera-grid",
    "evidence-figure",
    "review-row",
    "submit-wrap",
    "submit-reviews-button",
    "compose-wrap",
    "refined-query-input",
    "send-refined-button",
    "converged-banner",
    "pending-card",
    "export-thread-button",
)

# Review actions rendered by callbacks._review_column (reject is not shown).
VERDICT_ACTIONS: Tuple[str, ...] = ("confirm", "refine", "ignore")

LIVE_BADGE_TEXT = "live rag"


def dash_id_selector(component_id: Mapping[str, str]) -> str:
    """Return a CSS selector for a Dash pattern-matching (dict) component id.

    Dash's renderer writes dict ids into the DOM as JSON with sorted keys and no
    whitespace (``stringifyId``); json.dumps of that string yields a correctly
    escaped CSS attribute value.
    """
    flat = json.dumps(dict(sorted(component_id.items())), separators=(",", ":"))
    return f"[id={json.dumps(flat)}]"


def review_button_selector(camera_id: str, action: str) -> str:
    """Selector for one camera's Confirm/Refine/Ignore button."""
    if action not in VERDICT_ACTIONS:
        raise ValueError(f"unknown verdict action {action!r}")
    return dash_id_selector({"type": "review-btn", "cam": camera_id, "action": action})


def review_just_selector(camera_id: str) -> str:
    """Selector for the justification textarea a verdict click reveals."""
    return dash_id_selector({"type": "review-just", "cam": camera_id})


def is_live_badge(text: Optional[str]) -> bool:
    """True when the top-bar mode badge reads the live-mode label."""
    return " ".join((text or "").split()).casefold() == LIVE_BADGE_TEXT


# ---------------------------------------------------------------------------
# Questions and ground truth
# ---------------------------------------------------------------------------


@dataclass
class StudyTask:
    """One study task: a question matched to its ground-truth CSV row."""

    number: int
    question: str
    truth: str
    choices: List[str] = field(default_factory=list)
    anchor: str = ""
    qtype: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.question)

    @property
    def dir_name(self) -> str:
        return f"task{self.number:02d}_{self.slug}"


def normalize_question(text: str) -> str:
    """Case/space/quote-insensitive key for matching question text."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split()).casefold().rstrip("?").strip()


def load_ground_truth(csv_path: Path) -> List[Dict[str, str]]:
    """Read the QA CSV (columns ``Question``, ``Answer``, ``Distractor 1..3``…)."""
    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def match_questions(
    questions: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> List[StudyTask]:
    """Match each question to its CSV row by normalized question text.

    Tasks are numbered 1..N in the given order. Raises ``ValueError`` listing
    every question that has no row, so a typo fails before the browser starts.
    """
    index = {normalize_question(row.get("Question", "")): row for row in rows}
    tasks: List[StudyTask] = []
    missing: List[str] = []
    for number, question in enumerate(questions, start=1):
        row = index.get(normalize_question(question))
        if row is None:
            missing.append(question)
            continue
        truth = (row.get("Answer") or "").strip()
        distractors = [(row.get(f"Distractor {i}") or "").strip() for i in (1, 2, 3)]
        tasks.append(
            StudyTask(
                number=number,
                question=(row.get("Question") or question).strip(),
                truth=truth,
                choices=[truth, *[d for d in distractors if d]],
                anchor=(row.get("Anchor") or "").strip(),
                qtype=(row.get("Type") or "").strip(),
            )
        )
    if missing:
        raise ValueError(
            "questions not found in the ground-truth CSV: "
            + "; ".join(repr(q) for q in missing)
        )
    return tasks


def load_questions_arg(value: Optional[str]) -> List[str]:
    """Resolve ``--questions``: None -> candidates; a file -> one per line."""
    if not value:
        return list(CANDIDATE_QUESTIONS)
    path = Path(value)
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    return [q.strip() for q in value.split("|") if q.strip()]


def parse_task_selection(spec: Optional[str], n_tasks: int) -> List[int]:
    """Parse ``--tasks`` (``"1,3-5"``; empty = all) into sorted 1-based numbers."""
    if not spec or spec.strip().lower() == "all":
        return list(range(1, n_tasks + 1))
    chosen: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo > hi:
                raise ValueError(f"bad task range {part!r}")
            chosen.update(range(lo, hi + 1))
        else:
            chosen.add(int(part))
    bad = sorted(n for n in chosen if not 1 <= n <= n_tasks)
    if bad:
        raise ValueError(f"task numbers out of range 1..{n_tasks}: {bad}")
    return sorted(chosen)


def slugify(text: str, max_len: int = 40) -> str:
    """Filesystem-safe ASCII slug of ``text`` (``"When did Cathal want rösti?"``
    -> ``"when-did-cathal-want-rosti"``)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug or "task"


# ---------------------------------------------------------------------------
# Verdict plan
# ---------------------------------------------------------------------------


def load_verdict_plan(path: Optional[Path]) -> Dict[int, Dict[str, str]]:
    """Load a per-task verdict plan JSON: ``{"1": {"Bjorn": "confirm", ...}}``.

    Keys are task numbers; besides camera ids, ``"best"`` and ``"others"`` set
    the verdict for the top-scoring camera and for the rest.
    """
    if path is None:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    plan: Dict[int, Dict[str, str]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"plan entry for task {key!r} must be an object")
        for cam, action in entry.items():
            if action not in VERDICT_ACTIONS:
                raise ValueError(
                    f"task {key}: verdict {action!r} for {cam!r} not in "
                    f"{VERDICT_ACTIONS}"
                )
        plan[int(key)] = {str(k): str(v) for k, v in entry.items()}
    return plan


def top_camera(cameras: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Return the top-scoring camera id (scored angles first, then ``is_best``)."""
    if not cameras:
        return None
    ranked = sorted(
        cameras,
        key=lambda c: (
            not c.get("is_context", False),
            float(c.get("match_score") or 0.0),
            bool(c.get("is_best")),
        ),
        reverse=True,
    )
    return str(ranked[0]["camera_id"])


def resolve_verdicts(
    cameras: Sequence[Mapping[str, Any]],
    plan_entry: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Pick one verdict per displayed camera, in display order.

    Default: Confirm on the top-scoring camera, Refine on the others. A plan
    entry can override ``best`` / ``others`` or name individual cameras.
    """
    plan_entry = dict(plan_entry or {})
    best = top_camera(cameras)
    best_action = plan_entry.get("best", "confirm")
    other_action = plan_entry.get("others", "refine")
    verdicts: Dict[str, str] = {}
    for cam in cameras:
        cid = str(cam["camera_id"])
        default = best_action if cid == best else other_action
        verdicts[cid] = plan_entry.get(cid, default)
    return verdicts


# ---------------------------------------------------------------------------
# Answer text and truth matching
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def plain_answer_text(answer: str) -> str:
    """Answer prose without cite markers / markdown (as a reader sees it)."""
    text = CITE_MARKER_RE.sub(lambda m: f"{m.group(2)} {m.group(3)}", answer or "")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = text.replace("**", "")
    return " ".join(text.split())


def _normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [_NUMBER_WORDS.get(tok, tok) for tok in text.split()]
    return " ".join(tokens)


def answer_mentions_truth(answer: str, truth: str) -> bool:
    """Rough check: does the answer mention the ground truth?

    Case/accent/punctuation-insensitive whole-word substring match, with number
    words folded to digits ("four" == "4"). Heuristic only — the manifest flags
    every result for a manual check.
    """
    needle = _normalize_for_match(truth)
    if not needle:
        return False
    hay = _normalize_for_match(plain_answer_text(answer))
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


# ---------------------------------------------------------------------------
# Exported thread -> manifest entry
# ---------------------------------------------------------------------------


def _camera_watch_url(cam: Mapping[str, Any], mirror: YouTubeMirror) -> Optional[str]:
    day, hour = cam.get("day"), cam.get("hour")
    if day is None or hour is None:
        return cam.get("watch_url")  # type: ignore[return-value]
    camera_id = str(cam["camera_id"])
    if mirror.is_placeholder(str(day), camera_id, int(hour)):
        return None
    return mirror.watch_url(
        str(day), camera_id, int(hour), float(cam.get("start_seconds") or 0.0)
    )


def _camera_entry(cam: Mapping[str, Any], mirror: YouTubeMirror) -> Dict[str, Any]:
    return {
        "camera_id": cam.get("camera_id"),
        "day": cam.get("day"),
        "hour": cam.get("hour"),
        "start_seconds": cam.get("start_seconds"),
        "match_score": cam.get("match_score"),
        "is_best": cam.get("is_best"),
        "is_context": cam.get("is_context", False),
        "watch_url": _camera_watch_url(cam, mirror),
    }


def _find_moment(
    turn: Mapping[str, Any], moment_id: str
) -> Optional[Mapping[str, Any]]:
    for moment in turn.get("moments") or []:
        if moment.get("moment_id") == moment_id:
            return moment  # type: ignore[no-any-return]
    return None


def cited_moments(
    turn: Mapping[str, Any], mirror: YouTubeMirror
) -> List[Dict[str, Any]]:
    """Resolve the answer's ``[[cite:...]]`` markers to camera/time + watch URL.

    Marker moment ids are un-namespaced (``m0``); the export namespaces them
    with the group id (``g1-m0``), matching ``callbacks._citation_chip``.
    """
    out: List[Dict[str, Any]] = []
    gid = str(turn.get("group_id") or "")
    for match in CITE_MARKER_RE.finditer(str(turn.get("answer") or "")):
        mid, camera_id, label = match.group(1), match.group(2), match.group(3)
        moment = _find_moment(turn, f"{gid}-{mid}") or _find_moment(turn, mid)
        cam = next(
            (
                c
                for c in (moment or {}).get("cameras") or []
                if c.get("camera_id") == camera_id
            ),
            None,
        )
        entry: Dict[str, Any] = {
            "moment_id": (moment or {}).get("moment_id", mid),
            "camera_id": camera_id,
            "clock_label": label,
        }
        if cam is not None:
            details = _camera_entry(cam, mirror)
            details.pop("camera_id")
            entry.update(details)
        else:
            entry["watch_url"] = None
        out.append(entry)
    return out


def summarize_turn(
    turn: Optional[Mapping[str, Any]], mirror: YouTubeMirror
) -> Optional[Dict[str, Any]]:
    """Answer, claim, support, cited moments and displayed cameras of one turn.

    The displayed moment is the first (top-ranked) one: the UI focuses it on
    every new answer and the capture script clicks it for ``02_evidence``.
    """
    if not turn:
        return None
    claim = turn.get("claim") or {}
    moments = list(turn.get("moments") or [])
    top = moments[0] if moments else None
    answer = str(turn.get("answer") or "")
    return {
        "group_id": turn.get("group_id"),
        "iteration": turn.get("iteration"),
        "query": turn.get("refined_query") or turn.get("question"),
        "answer_text": plain_answer_text(answer),
        "answer_raw": answer,
        "claim": claim.get("text"),
        "support": claim.get("support"),
        "cited_moments": cited_moments(turn, mirror),
        "top_moment": (
            {
                "moment_id": top.get("moment_id"),
                "clock_label": top.get("clock_label"),
                "place_label": top.get("place_label"),
                "aggregate_score": top.get("aggregate_score"),
            }
            if top
            else None
        ),
        "displayed_cameras": [
            _camera_entry(c, mirror) for c in ((top or {}).get("cameras") or [])
        ],
        "reviews": turn.get("reviews") or {},
        "sent_refined_query": turn.get("sent_refined_query"),
    }


def split_turns(
    export: Mapping[str, Any],
) -> Tuple[Optional[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Return (initial answer turn, refinement turns) of a one-task export."""
    turns = list(export.get("turns") or [])
    initial = next((t for t in turns if not t.get("is_refinement")), None)
    refined = [t for t in turns if t.get("is_refinement")]
    return initial, refined


def build_task_entry(
    task: StudyTask,
    export: Mapping[str, Any],
    mirror: YouTubeMirror,
    *,
    mode: str,
    task_dir: str,
    screenshots: Iterable[str] = (),
    verdicts: Optional[Mapping[str, str]] = None,
    converged: bool = False,
) -> Dict[str, Any]:
    """Build one manifest entry from the task and its exported thread."""
    initial_turn, refined_turns = split_turns(export)
    initial = summarize_turn(initial_turn, mirror) or {}
    refined = summarize_turn(refined_turns[-1], mirror) if refined_turns else None
    answer_raw = str(initial.get("answer_raw") or "")
    return {
        "task": task.number,
        "dir": task_dir,
        "question": task.question,
        "ground_truth": task.truth,
        "choices": task.choices,
        "anchor": task.anchor,
        "type": task.qtype,
        "mode": mode,
        "offline_fabricated": mode != "live",
        "answer_text": initial.get("answer_text"),
        "claim": initial.get("claim"),
        "support": initial.get("support"),
        "cited_moments": initial.get("cited_moments", []),
        "top_moment": initial.get("top_moment"),
        "displayed_cameras": initial.get("displayed_cameras", []),
        "answer_mentions_truth": answer_mentions_truth(answer_raw, task.truth),
        "answer_mentions_truth_needs_manual_check": True,
        "verdicts": dict(verdicts or {}),
        "converged_without_refine": converged,
        "refined": refined,
        "refined_mentions_truth": (
            answer_mentions_truth(str(refined.get("answer_raw") or ""), task.truth)
            if refined
            else None
        ),
        "screenshots": list(screenshots),
    }


def merge_entries(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    rerun: Iterable[int] = (),
) -> List[Dict[str, Any]]:
    """Merge manifest entries by task number; ``current`` wins, sorted by task.

    Prior entries for tasks in ``rerun`` are dropped even when this run produced
    no replacement (the task failed), so they can't masquerade as current.
    """
    dropped = {int(n) for n in rerun}
    merged: Dict[int, Dict[str, Any]] = {
        int(e["task"]): dict(e)
        for e in previous
        if "task" in e and int(e["task"]) not in dropped
    }
    merged.update({int(e["task"]): dict(e) for e in current})
    return [merged[k] for k in sorted(merged)]


def build_manifest(
    entries: Sequence[Mapping[str, Any]],
    *,
    ui_url: str,
    mode: str,
    created: str,
    footage_masked: bool = True,
) -> Dict[str, Any]:
    """Wrap per-task entries with run metadata."""
    return {
        "created": created,
        "footage_masked": footage_masked,
        "ui_url": ui_url,
        "mode": mode,
        "note": (
            "answer_mentions_truth is a normalized substring/number heuristic; "
            "check every task by hand."
            + (
                ""
                if mode == "live"
                else " OFFLINE RUN: answers are FABRICATED placeholders — "
                "never use them in the study."
            )
        ),
        "tasks": list(entries),
    }


# ---------------------------------------------------------------------------
# Orientation callouts
# ---------------------------------------------------------------------------

# (number, label, selector) for 00_orientation.png. The group card is resolved
# to the last card in the thread by the capture script.
ORIENTATION_TARGETS: Tuple[Tuple[int, str, str], ...] = (
    (1, "Question, answer, claim + support badge", SEL_GROUP_CARD),
    (2, "Synchronized cameras", SEL_CAMERA_GRID),
    (3, "Confirm / Refine / Ignore", SEL_REVIEW_ROW),
    (4, "Refined search", SEL_COMPOSE_WRAP),
)

Box = Tuple[float, float, float, float]  # x, y, width, height (CSS px)


@dataclass
class Callout:
    """One numbered callout in image pixels."""

    number: int
    label: str
    rect: Tuple[int, int, int, int]  # x0, y0, x1, y1
    badge_center: Tuple[int, int]
    badge_radius: int


def callout_layout(
    targets: Sequence[Tuple[int, str, Optional[Box]]],
    image_size: Tuple[int, int],
    *,
    scale: float = 1.0,
    pad: int = 6,
    badge_radius: int = 22,
) -> List[Callout]:
    """Convert element bounding boxes (CSS px) to clamped callouts (image px).

    ``scale`` is the screenshot's device-scale factor. Targets without a box, or
    whose box falls entirely outside the image, are skipped. The number badge
    sits just outside the box's top-left corner (left of it, else above it) so
    it never hides the element's own label; it falls back to inside the corner
    when the box hugs the image edge.
    """
    width, height = image_size
    radius = int(round(badge_radius * scale))
    out: List[Callout] = []
    for number, label, box in targets:
        if box is None:
            continue
        x, y, w, h = box
        x0 = int(round((x - pad) * scale))
        y0 = int(round((y - pad) * scale))
        x1 = int(round((x + w + pad) * scale))
        y1 = int(round((y + h + pad) * scale))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width - 1, x1), min(height - 1, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        gap = max(2, radius // 5)
        if x0 - 2 * radius - gap >= 0:  # left of the corner
            cx, cy = x0 - radius - gap, y0 + radius
        elif y0 - 2 * radius - gap >= 0:  # above the corner
            cx, cy = x0 + radius, y0 - radius - gap
        else:  # inside the corner
            cx, cy = x0 + radius, y0 + radius
        cx = min(max(cx, radius), width - radius - 1)
        cy = min(max(cy, radius), height - radius - 1)
        out.append(Callout(number, label, (x0, y0, x1, y1), (cx, cy), radius))
    return out


def annotate_image(
    src: Path,
    dst: Path,
    callouts: Sequence[Callout],
    *,
    color: Tuple[int, int, int] = (230, 57, 70),
) -> None:
    """Draw numbered callouts (outline + filled number badge) onto a screenshot."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(image)
    for co in callouts:
        stroke = max(3, co.badge_radius // 6)
        draw.rectangle(co.rect, outline=color, width=stroke)
        cx, cy = co.badge_center
        r = co.badge_radius
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=color,
            outline="white",
            width=max(2, stroke // 2),
        )
        font_size = int(r * 1.3)
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:  # Pillow < 10.1 has no sized default font
            font = ImageFont.load_default()
        draw.text((cx, cy), str(co.number), fill="white", font=font, anchor="mm")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    image.save(dst)


# ---------------------------------------------------------------------------
# Export for the Google Form (scripts/study_form.gs)
# ---------------------------------------------------------------------------

FORM_SLOTS: Tuple[str, ...] = ("practice", "t1", "t2", "t3", "t4")
# What each slot must show, per the study's selection rule. Used only to warn.
FORM_SLOT_SHOULD_BE_RIGHT: Dict[str, bool] = {
    "practice": True,
    "t1": True,
    "t2": True,
    "t3": True,
    "t4": False,
}
FORM_STEPS: Tuple[str, ...] = ("01_answer", "02_evidence", "03_review", "04_refined")


def parse_slots(spec: str) -> Dict[str, int]:
    """``"practice=3,t1=8,t2=9,t3=7,t4=4"`` -> ``{"practice": 3, ...}``.

    Every form slot must be assigned exactly once, to a distinct task.
    """
    slots: Dict[str, int] = {}
    for part in (spec or "").split(","):
        if not part.strip():
            continue
        name, sep, value = part.partition("=")
        name = name.strip()
        if not sep or name not in FORM_SLOTS:
            raise ValueError(
                f"bad slot {part!r}; expected one of {FORM_SLOTS} as name=task"
            )
        if name in slots:
            raise ValueError(f"slot {name!r} assigned twice")
        slots[name] = int(value)
    missing = [s for s in FORM_SLOTS if s not in slots]
    if missing:
        raise ValueError(f"missing slots: {missing}")
    if len(set(slots.values())) != len(slots):
        raise ValueError("each slot needs a different task")
    return slots


def form_export_plan(
    manifest: Mapping[str, Any], slots: Mapping[str, int]
) -> Dict[str, Any]:
    """Work out what to copy for the Google Form and what to paste into it.

    Returns ``copies`` (``(source relative to the capture dir, form file
    name)``), ``tasks`` and ``footage`` (slot -> question / camera -> URL, the
    ``TASKS`` and ``FOOTAGE`` constants of ``scripts/study_form.gs``) and
    ``warnings`` (offline captures, missing steps, missing footage links, and
    answers that don't fit their slot by the ``answer_mentions_truth``
    heuristic). Raises ``KeyError`` for a slot whose task isn't in the manifest.
    """
    by_task = {int(e["task"]): e for e in manifest.get("tasks", [])}
    if not manifest.get("footage_masked"):
        warnings_pre = [
            "screenshots show unblurred dataset footage (captured with "
            "--show-footage); do not put them in the participant form"
        ]
    else:
        warnings_pre = []
    copies: List[Tuple[str, str]] = []
    tasks: Dict[str, Dict[str, str]] = {}
    footage: Dict[str, Dict[str, str]] = {}
    warnings: List[str] = list(warnings_pre)
    orientation: Optional[str] = None
    for task_entry in by_task.values():
        for shot in task_entry.get("screenshots") or []:
            if Path(str(shot)).name == "00_orientation.png" and orientation is None:
                orientation = str(Path(str(task_entry["dir"])) / "00_orientation.png")
    if orientation:
        copies.append((orientation, "orientation.png"))
    else:
        warnings.append(
            "no 00_orientation.png in the manifest; rerun with --orientation"
        )

    for slot in FORM_SLOTS:
        number = int(slots[slot])
        if number not in by_task:
            raise KeyError(f"slot {slot}: task {number} is not in the manifest")
        entry = by_task[number]
        tasks[slot] = {"question": str(entry["question"])}
        if entry.get("mode") != "live":
            warnings.append(
                f"{slot} (task {number}) was captured offline; its answer is fabricated"
            )
        names = {Path(str(s)).name for s in entry.get("screenshots") or []}
        for step in FORM_STEPS:
            if f"{step}_crop.png" in names:  # readable at the form's image width
                copies.append(
                    (
                        str(Path(str(entry["dir"])) / f"{step}_crop.png"),
                        f"{slot}_{step}.png",
                    )
                )
            elif f"{step}.png" in names:
                copies.append(
                    (str(Path(str(entry["dir"])) / f"{step}.png"), f"{slot}_{step}.png")
                )
            else:
                warnings.append(f"{slot} (task {number}) has no {step}.png")
        links = {
            str(c["camera_id"]): str(c["watch_url"])
            for c in entry.get("displayed_cameras") or []
            if c.get("watch_url") and not c.get("is_context")
        }
        if not links:
            warnings.append(f"{slot} (task {number}) has no footage links")
        footage[slot] = links
        right = entry.get("answer_mentions_truth")
        if right is not None and bool(right) != FORM_SLOT_SHOULD_BE_RIGHT[slot]:
            want = "right" if FORM_SLOT_SHOULD_BE_RIGHT[slot] else "wrong"
            warnings.append(
                f"{slot} (task {number}) should be {want} but "
                f"answer_mentions_truth={right}; check by hand"
            )
    return {"copies": copies, "tasks": tasks, "footage": footage, "warnings": warnings}


def form_constants_js(plan: Mapping[str, Any]) -> str:
    """The ``TASKS`` and ``FOOTAGE`` constants for ``scripts/study_form.gs``."""
    tasks = json.dumps(plan["tasks"], indent=2, ensure_ascii=False)
    footage = json.dumps(plan["footage"], indent=2, ensure_ascii=False)
    return f"const TASKS = {tasks};\n\nconst FOOTAGE = {footage};\n"
