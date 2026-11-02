"""Measure where a literature project's semantic bar belongs.

`search.library` counts a semantic hit only when a question's best passage
stands `LIBRARY_SEMANTIC_GAP` above its median one. The default, 0.12, was
measured on two NLP papers. A real project, forty papers on one narrow subject,
bunches every similarity together, and the bar that kept the Eiffel Tower out
of two papers may also keep a fair question out of forty. This measures it on
your own papers instead of guessing.

Write two lists of questions, one per line, in the language you ask in:

    on.txt   questions your papers DO answer
    off.txt  questions they do not (a recipe, a share price, another field)

then, with the app's database and the profile's embedding model:

    uv run python tools/calibrate_library.py --db data/simplixity.db \
        --project "Masterarbeit" --on on.txt --off off.txt

It prints each question's gap and a suggested bar halfway between the closest
off-topic and the weakest on-topic question. Set it with LIBRARY_SEMANTIC_GAP.
Only the questions are sent to the embedding provider; the passages' vectors
are already stored (a stale index is embedded first, exactly as a run would).
In Docker, with the question files copied in first:

    docker compose cp on.txt simplixity:/tmp/on.txt
    docker compose cp off.txt simplixity:/tmp/off.txt
    # -u: as the app's user, or SQLite's journal files end up owned by root
    docker compose exec -u simplixity simplixity python tools/calibrate_library.py \
        --db /data/simplixity.db --project "Masterarbeit" --on /tmp/on.txt --off /tmp/off.txt
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.search.library import Library
from app.store import db as db_module
from app.store import projects, settings_store


def _questions(path: str) -> list[str]:
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


async def _gaps(library: Library, questions: list[str]) -> list[float]:
    gaps = []
    for question in questions:
        before = len(library.probes)
        await library.search([question], limit=3)
        probe = library.probes[before] if len(library.probes) > before else None
        if probe is None:
            print(f"  (no semantic search ran)  {question}")
            continue
        gap = probe["top"] - probe["median"]
        gaps.append(gap)
        print(f"  gap {gap:.3f}  top {probe['top']:.3f}  median {probe['median']:.3f}  {question}")
    return gaps


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--project", required=True, help="project name or id")
    parser.add_argument("--on", required=True, help="questions the papers answer")
    parser.add_argument("--off", required=True, help="questions they do not")
    parser.add_argument("--profile", default=db_module.DEFAULT_USER)
    args = parser.parse_args()

    conn = await db_module.connect(args.db)
    try:
        found = [
            p
            for p in await projects.list_projects(conn, user=args.profile)
            if args.project in (p["id"], p["name"])
        ]
        if not found:
            print(f"No project {args.project!r} for profile {args.profile!r}.")
            return 1
        saved = await settings_store.load(conn, args.profile)
        if not (saved.embedding_enabled and saved.embedding_model):
            print("This profile has no embedding model; there is no semantic bar to set.")
            return 1
        library = Library(
            conn,
            found[0]["id"],
            embedding_model=saved.embedding_model,
            embedding_provider=saved.embedding_provider,
        )
        pages, abstracts = await library.size()
        print(f"{found[0]['name']}: {pages} pages, {abstracts} abstracts · {saved.embedding_model}")
        print("On topic:")
        on = await _gaps(library, _questions(args.on))
        print("Off topic:")
        off = await _gaps(library, _questions(args.off))
        for note in dict.fromkeys(library.notes):
            print(f"note: {note}")
    finally:
        await conn.close()

    if not on or not off:
        print("Need gaps for both lists to suggest a bar.")
        return 1
    weakest, closest = min(on), max(off)
    print(
        f"\nOn topic: weakest {weakest:.3f}, median {statistics.median(on):.3f}. "
        f"Off topic: closest {closest:.3f}, median {statistics.median(off):.3f}."
    )
    if closest >= weakest:
        print(
            "The two overlap: no single bar separates them. Pick one that keeps the "
            "on-topic questions you care about, and expect the odd off-topic hit."
        )
        return 0
    print(f"Suggested LIBRARY_SEMANTIC_GAP={(weakest + closest) / 2:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
