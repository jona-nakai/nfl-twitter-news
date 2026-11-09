"""Score models on hand-labeled posts (evals/cases.json): routing accuracy, cost, and latency.

Each case is a post plus the channels it should land in ([] = not posted: not news, too minor,
or a duplicate of one of the case's already-posted stories). Grading is on the outcome, the
channel list, because that's what members see and get pinged for.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import statistics
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .classifier import PRICES, Classifier, Decision, Usage
from .config import ALL_NEWS, BREAKING, Settings
from .db import Story
from .pings import ordered
from .x_api import Tweet

DEFAULT_MODELS = ("gpt-6-luna", "claude-haiku-5-5", "gpt-6.1-sol")
RESULTS_DIR = Path("evals/results")


@dataclass
class Case:
    id: str
    author: str
    text: str
    expect: list[str]
    also_ok: list[list[str]] = field(default_factory=list)
    stories: list[dict] = field(default_factory=list)
    quoted: str | None = None
    parent: str | None = None
    duplicate_of: int | None = None
    image: str | None = None  # URL, or a path to a local file
    label: bool | None = None  # expected breaking_label, when the case is about labels
    note: str | None = None

    def accepted(self) -> list[list[str]]:
        """Every posted story also goes to all-news, so cases needn't list it."""
        return [ordered({*c, ALL_NEWS}) if c else [] for c in (self.expect, *self.also_ok)]


@dataclass
class Run:
    case: Case
    decision: Decision | None
    usage: list[Usage]
    error: str | None = None  # API failure

    @property
    def channels(self) -> list[str] | None:
        return self.decision.channels() if self.decision else None

    @property
    def mistake(self) -> str | None:
        """None if the post was routed correctly, else what went wrong."""
        if self.error:
            return "API error"
        got = self.channels
        accepted = self.case.accepted()
        if got in accepted:
            if self.case.label is not None and self.decision.labeled != self.case.label:
                return "missed breaking label" if self.case.label else "false breaking label"
            return None
        if not any(accepted) and got:
            return "posted a duplicate" if self.case.duplicate_of else "posted non-news"
        if all(accepted) and not got:
            return "wrongly marked duplicate" if self.decision.duplicate_of else "dropped news"
        if BREAKING in got and not any(BREAKING in a for a in accepted):
            return "false breaking"
        if BREAKING not in got and all(BREAKING in a for a in accepted):
            return "missed breaking"
        return "wrong topic channel"

    @property
    def cost(self) -> float | None:
        costs = [u.cost for u in self.usage]
        return None if None in costs else sum(costs)

    @property
    def cold_cost(self) -> float | None:
        costs = [u.cold_cost for u in self.usage]
        return None if None in costs else sum(costs)

    @property
    def seconds(self) -> float:
        return sum(u.seconds for u in self.usage)


def load_cases(path: Path) -> list[Case]:
    return [Case(**raw) for raw in json.loads(path.read_text())]


def case_inputs(case: Case, now: datetime) -> tuple[Tweet, list[Story]]:
    tweet = Tweet(
        id=f"eval-{case.id}",
        author_id="author",
        username=case.author,
        author_name=case.author,
        avatar_url=None,
        text=case.text,
        created_at=now,
        reply_to_user_id="author" if case.parent else None,
        quoted_text=case.quoted,
        parent_text=case.parent,
        image_url=_image_url(case.image),
        simulated=True,
    )
    stories = [
        Story(
            id=s["id"],
            created_at=now - timedelta(minutes=s["minutes_ago"]),
            headline=s["headline"],
            channels=tuple(s["channels"]),
            first_author=s["author"],
        )
        for s in sorted(case.stories, key=lambda s: -s["minutes_ago"])
    ]
    return tweet, stories


def rough_estimate(models: list[str], n: int) -> float:
    """Upper bound before running: ~1.6K uncached input and ~150 output tokens per call."""
    total = 0.0
    for model in models:
        inp, _, out = PRICES.get(model, (0, 0, 0))
        total += n * (1600 * inp + 150 * out) / 1e6
    return total


async def run_eval(
    settings: Settings,
    models: list[str],
    cases: list[Case],
    *,
    repeat: int = 1,
    concurrency: int = 6,
) -> dict[str, list[Run]]:
    openai_client = claude_client = None
    every_model = set(models)
    if any(not m.startswith("claude-") for m in every_model):
        from openai import AsyncOpenAI

        openai_client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=3, timeout=180)
    if any(m.startswith("claude-") for m in every_model):
        from anthropic import AsyncAnthropic

        claude_client = AsyncAnthropic(
            api_key=settings.anthropic_api_key or None, max_retries=3, timeout=180
        )

    limit = asyncio.Semaphore(concurrency)
    now = datetime.now(UTC)

    async def one(model: str, case: Case) -> Run:
        usage: list[Usage] = []
        classifier = Classifier(
            replace(settings, openai_model=model),
            openai_client,
            claude_client,
            on_usage=usage.append,
        )
        tweet, stories = case_inputs(case, now)
        async with limit:
            try:
                decision = await classifier.classify(tweet, stories)
            except Exception as e:  # report and keep going
                return Run(case, None, usage, error=f"{type(e).__name__}: {e}")
        return Run(case, decision, usage)

    results = {}
    for spec in models:
        print(f"Running {spec} on {len(cases)} posts x{repeat}...", flush=True)
        results[spec] = await asyncio.gather(
            *(one(spec, case) for case in cases for _ in range(repeat))
        )
    return results


def report(results: dict[str, list[Run]]) -> str:
    """A summary table, then every mistake with the model's stated reason."""
    lines = []
    header = (
        f"{'setup':<24} {'correct':>8} {'false brk':>9} {'missed brk':>10} {'other':>6} "
        f"{'$/1k warm':>10} {'$/1k cold':>10} {'avg s':>6} {'max s':>6}"
    )
    lines += [header, "-" * len(header)]
    for spec, runs in results.items():
        mistakes = [r.mistake for r in runs]
        correct = mistakes.count(None)
        false_brk = mistakes.count("false breaking")
        missed_brk = mistakes.count("missed breaking")
        other = len(runs) - correct - false_brk - missed_brk
        secs = [r.seconds for r in runs if not r.error]
        lines.append(
            f"{spec:<24} {f'{correct}/{len(runs)}':>8} {false_brk:>9} {missed_brk:>10} "
            f"{other:>6} {_per_1k([r.cost for r in runs if not r.error]):>10} "
            f"{_per_1k([r.cold_cost for r in runs if not r.error]):>10} "
            f"{statistics.mean(secs) if secs else 0:>6.1f} {max(secs, default=0):>6.1f}"
        )
    lines.append(
        "warm = as measured, with the instructions cached; cold = nothing cached, as when "
        "posts are far apart."
    )
    for spec, runs in results.items():
        wrong = [r for r in runs if r.mistake]
        if not wrong:
            continue
        lines += ["", f"{spec} mistakes:"]
        for r in wrong:
            want = " or ".join(_fmt(a) for a in r.case.accepted())
            got = r.error or _fmt(r.channels)
            reason = f" ({r.decision.reason})" if r.decision else ""
            lines.append(f"  {r.case.id}: {r.mistake}. Wanted {want}, got {got}{reason}")
    total = sum(r.cost or 0 for runs in results.values() for r in runs)
    lines += ["", f"Total spent on this run: ${total:.4f}"]
    return "\n".join(lines)


def save(results: dict[str, list[Run]]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    data = {
        spec: [
            {
                "case": r.case.id,
                "mistake": r.mistake,
                "expected": r.case.accepted(),
                "channels": r.channels,
                "decision": r.decision.model_dump() if r.decision else None,
                "error": r.error,
                "cost": r.cost,
                "cold_cost": r.cold_cost,
                "seconds": round(r.seconds, 2),
                "usage": [vars(u) for u in r.usage],
            }
            for r in runs
        ]
        for spec, runs in results.items()
    }
    path.write_text(json.dumps(data, indent=2))
    return path


def _image_url(image: str | None) -> str | None:
    if not image or image.startswith(("http://", "https://")):
        return image
    mime = mimetypes.guess_type(image)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(image).read_bytes()).decode()


def _per_1k(costs: list[float | None]) -> str:
    if not costs or None in costs:
        return "?"
    return f"${statistics.mean(costs) * 1000:.2f}"


def _fmt(channels: list[str] | None) -> str:
    return "[" + ", ".join(channels or []) + "]"
