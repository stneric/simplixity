"""The image proxy: what it fetches on the browser's behalf, and at what size."""

from __future__ import annotations

import pytest

from app.api import media

PORTRAIT = (
    "https://upload.wikimedia.org/wikipedia/commons/5/56/Donald_Trump_official_portrait.jpg"
    "?utm_source=de.wikipedia.org&utm_campaign=index"
)
PORTRAIT_THUMB = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/5/56/"
    "Donald_Trump_official_portrait.jpg/960px-Donald_Trump_official_portrait.jpg"
)


def test_a_commons_original_is_fetched_as_its_thumbnail():
    """Observed on "wer ist donald trump": the card chose the official portrait,
    a 5.9 MB original, the size cap turned it into a blank pixel, and the card
    dropped its portrait."""
    assert media._commons_thumbnail(PORTRAIT) == PORTRAIT_THUMB


def test_an_svg_thumbnail_is_the_rendered_png():
    url = "https://upload.wikimedia.org/wikipedia/commons/1/1a/Flag_of_Argentina.svg"
    assert media._commons_thumbnail(url) == (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1a/"
        "Flag_of_Argentina.svg/960px-Flag_of_Argentina.svg.png"
    )


@pytest.mark.parametrize(
    "url",
    [
        # Already a thumbnail.
        PORTRAIT_THUMB,
        # A format whose thumbnail is named differently.
        "https://upload.wikimedia.org/wikipedia/commons/a/ab/Scan.tif",
        # Not Commons at all.
        "https://example.com/wikipedia/commons/5/56/Photo.jpg",
        "https://upload.wikimedia.org/math/render/svg/abc",
    ],
)
def test_anything_else_is_left_alone(url):
    assert media._commons_thumbnail(url) == ""


async def test_the_thumbnail_is_tried_first_and_the_original_is_the_fallback(monkeypatch):
    asked = []

    async def get_typed(url, limit):
        asked.append(url)
        if url == PORTRAIT_THUMB:
            return b"", ""
        return b"jpeg", "image/jpeg"

    monkeypatch.setattr(media, "_get_typed", get_typed)
    response = await media.image(PORTRAIT)

    assert asked == [PORTRAIT_THUMB, PORTRAIT]
    assert response.body == b"jpeg"


async def test_a_thumbnail_that_answers_is_the_only_fetch(monkeypatch):
    asked = []

    async def get_typed(url, limit):
        asked.append(url)
        return b"jpeg", "image/jpeg"

    monkeypatch.setattr(media, "_get_typed", get_typed)
    await media.image(PORTRAIT)

    assert asked == [PORTRAIT_THUMB]


async def test_a_picture_that_could_not_be_fetched_is_not_cached(monkeypatch):
    """Observed: the blank pixel was cached for an hour, so after the size fix
    the browser kept showing nothing for the same portrait URL."""

    async def get_typed(url, limit):
        return b"", ""

    monkeypatch.setattr(media, "_get_typed", get_typed)
    response = await media.image(PORTRAIT)

    assert response.body == media.BLANK
    assert response.headers["cache-control"] == "no-store"
