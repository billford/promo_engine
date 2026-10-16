import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import images
from bluesky import post_to_bluesky
from db import get_conn, get_recent_image_ids, init_db, insert_post_record
from images import PEOPLE_RE, PRIVATE_RE, PostImage, build_tsquery, find_image


def _py_regex(pg_regex: str) -> re.Pattern:
    """Postgres \\m/\\M word boundaries -> Python \\b, to sanity-check the word lists."""
    return re.compile(pg_regex.replace(r"\m", r"\b").replace(r"\M", r"\b"), re.IGNORECASE)


@pytest.mark.parametrize("description", [
    "A young child is sitting in a stroller.",
    "Performers in the center of the frame on an ice rink.",
    "A character in a dark forest, dressed in a black cloak.",
    "Two hands hold a coffee mug.",
    "A poster for the band with a collage of photographs.",
])
def test_people_filter_catches_people(description):
    assert _py_regex(PEOPLE_RE).search(description)


@pytest.mark.parametrize("description", [
    "A screenshot of a settings menu.",
    "A snow-covered backyard with a grill.",
    "A residential street in winter.",
    "A starry night sky with a photographer's watermark in the corner.",
])
def test_private_filter_catches_private_scenes(description):
    assert _py_regex(PRIVATE_RE).search(description)


@pytest.mark.parametrize("description", [
    "A misty forest scene shrouded in fog.",
    "A close-up view of a computer keyboard with green illuminated keys.",
    "A black dog standing on rocks near a stream.",
])
def test_filters_keep_plain_scenes(description):
    assert not _py_regex(PEOPLE_RE).search(description)
    assert not _py_regex(PRIVATE_RE).search(description)


@pytest.mark.parametrize("raw", [
    '{"photo_id": 7, "alt_text": "fog"}',
    'Here you go:\n{"photo_id": 7, "alt_text": "fog"}\n\nThis fits because {reason}.',
    '```json\n{"photo_id": 7, "alt_text": "fog"}\n```\n{"photo_id": 8}',
])
def test_parse_json_ignores_surrounding_text(raw):
    assert images._parse_json(raw, "{") == {"photo_id": 7, "alt_text": "fog"}  # pylint: disable=protected-access


def test_parse_json_raises_on_no_json():
    with pytest.raises(RuntimeError):
        images._parse_json("nothing fits", "{")  # pylint: disable=protected-access


def test_build_tsquery_strips_query_syntax():
    assert build_tsquery(["empty chair", "fog!", "a' | b:*", "", "123", "chair"]) == \
        "(empty & chair) | (chair) | (fog) | (a & b) | (b)"


def test_find_image_disabled_without_photo_db_url():
    assert find_image({"title": "t"}, {"anthropic_api_key": "k"}, set()) is None


def test_find_image_never_raises(capsys):
    with patch("images._find_image", side_effect=OSError("no route to host")):
        assert find_image({"title": "t"}, {"photo_db_url": "postgresql://x"}, set()) is None
    assert "no image attached" in capsys.readouterr().err


def _client_replying(*texts):
    client = MagicMock()
    client.messages.create.side_effect = [MagicMock(content=[MagicMock(text=t)]) for t in texts]
    return client


_CONFIG = {"photo_db_url": "postgresql://x", "anthropic_api_key": "k"}
_CONTENT = {"title": "On Burnout", "description": "An essay."}


def _src(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"raw")
    return str(path)


def _verdict(ok, alt="Fog over a lake", reason="clean"):
    return json.dumps({"ok": ok, "reason": reason, "alt_text": alt})


def _fake_prepare(_src_path, dest):
    from PIL import Image

    Image.new("RGB", (400, 300)).save(dest, "JPEG")
    return (2000, 1500)


def test_find_image_skips_id_outside_candidates(tmp_path, capsys):
    client = _client_replying('["fog"]', json.dumps({"photo_ids": [999]}))
    candidates = [{"id": 1, "nas_path": _src(tmp_path, "a.jpg"), "description": "fog"}]
    with patch("images.anthropic.Anthropic", return_value=client), \
            patch("images.search_candidates", return_value=candidates), \
            patch("images.prepare_file") as prepare:
        assert find_image(_CONTENT, {**_CONFIG, "image_dir": str(tmp_path)}, set()) is None
    prepare.assert_not_called()
    assert "not a candidate" in capsys.readouterr().err


def test_find_image_passes_when_nothing_fits(tmp_path):
    client = _client_replying('["fog"]', json.dumps({"photo_ids": [], "reason": "no fit"}))
    candidates = [{"id": 1, "nas_path": _src(tmp_path, "a.jpg"), "description": "fog"}]
    with patch("images.anthropic.Anthropic", return_value=client), \
            patch("images.search_candidates", return_value=candidates), \
            patch("images.prepare_file") as prepare:
        assert find_image(_CONTENT, {**_CONFIG, "image_dir": str(tmp_path)}, set()) is None
    prepare.assert_not_called()


def test_find_image_success(tmp_path):
    client = _client_replying('Sure: ["fog", "bench"]', json.dumps({"photo_ids": [7]}), _verdict(True))
    candidates = [{"id": 7, "nas_path": _src(tmp_path, "src.dng"), "description": "Fog over a lake."}]
    with patch("images.anthropic.Anthropic", return_value=client), \
            patch("images.search_candidates", return_value=candidates) as search, \
            patch("images.prepare_file", side_effect=_fake_prepare):
        image = find_image(_CONTENT, {**_CONFIG, "image_dir": str(tmp_path / "out")}, {3})
    assert image == PostImage(7, (tmp_path / "out" / "7.jpg").resolve(), "Fog over a lake", 2000, 1500)
    assert search.call_args.args[1:] == (["fog", "bench"], {3})


def test_visual_check_rejection_falls_back_to_next_pick(tmp_path, capsys):
    client = _client_replying(
        '["shark"]',
        json.dumps({"photo_ids": [5, 6]}),
        _verdict(False, reason="A person is reflected in the aquarium glass."),
        _verdict(True, alt="A misty waterfall"),
    )
    candidates = [
        {"id": 5, "nas_path": _src(tmp_path, "a.jpg"), "description": "An aquarium."},
        {"id": 6, "nas_path": _src(tmp_path, "b.jpg"), "description": "A waterfall."},
    ]
    out = tmp_path / "out"
    with patch("images.anthropic.Anthropic", return_value=client), \
            patch("images.search_candidates", return_value=candidates), \
            patch("images.prepare_file", side_effect=_fake_prepare):
        image = find_image(_CONTENT, {**_CONFIG, "image_dir": str(out)}, set())
    assert (image.photo_id, image.alt_text) == (6, "A misty waterfall")
    assert not (out / "5.jpg").exists()  # rejected file is not left lying around
    assert "Rejected photo #5" in capsys.readouterr().out


@pytest.mark.parametrize("reply", ['{"ok": "yes"}', '{"reason": "unsure"}', '{"ok": null}'])
def test_visual_check_requires_explicit_true(tmp_path, reply):
    from PIL import Image

    photo = tmp_path / "p.jpg"
    Image.new("RGB", (300, 200)).save(photo)
    ok, _, _ = images.verify_image(_client_replying(reply), photo)
    assert ok is False


def test_visual_check_sends_full_image_plus_zoomed_tiles(tmp_path):
    from PIL import Image

    photo = tmp_path / "p.jpg"
    Image.new("RGB", (1333, 2000)).save(photo)
    client = _client_replying(_verdict(True))
    images.verify_image(client, photo)
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    blocks = [b for b in content if b["type"] == "image"]
    assert len(blocks) == 1 + images.TILE_GRID ** 2


def test_all_picks_rejected_returns_none(tmp_path):
    client = _client_replying('["fog"]', json.dumps({"photo_ids": [1, 2, 3, 4]}),
                              _verdict(False), _verdict(False), _verdict(False))
    candidates = [{"id": i, "nas_path": _src(tmp_path, f"{i}.src"), "description": "fog"} for i in (1, 2, 3, 4)]
    with patch("images.anthropic.Anthropic", return_value=client), \
            patch("images.search_candidates", return_value=candidates), \
            patch("images.prepare_file", side_effect=_fake_prepare) as prepare:
        assert find_image(_CONTENT, {**_CONFIG, "image_dir": str(tmp_path / "out")}, set()) is None
    assert prepare.call_count == images.MAX_PICKS


@pytest.mark.skipif(sys.platform != "darwin", reason="uses macOS sips")
def test_prepare_file_strips_gps_and_applies_orientation(tmp_path):
    from PIL import Image

    src = tmp_path / "src.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW
    exif[0x8825] = {1: "N", 2: (40.0, 26.0, 46.0)}  # GPS IFD
    Image.new("RGB", (3000, 1500), "red").save(src, exif=exif)

    width, height = images.prepare_file(src, tmp_path / "out.jpg")

    out = Image.open(tmp_path / "out.jpg")
    assert (width, height) == out.size == (1000, 2000)
    assert not dict(out.getexif())
    assert (tmp_path / "out.jpg").stat().st_size <= images.BLUESKY_MAX_BYTES


def test_recent_image_ids_ignore_dry_runs(tmp_path):
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    with get_conn(db_path, enforce_fk=False) as conn:
        insert_post_record(conn, "medium:a", "bluesky", "text", image_id=11)
        insert_post_record(conn, "medium:b", "bluesky", "text", dry_run=True, image_id=12)
        insert_post_record(conn, "medium:c", "facebook", "text")
        assert get_recent_image_ids(conn, 90) == {11}


@patch("bluesky.requests.post")
def test_bluesky_attaches_image(mock_post, tmp_path):
    photo = tmp_path / "7.jpg"
    photo.write_bytes(b"jpegbytes")
    session = MagicMock(status_code=200)
    session.json.return_value = {"accessJwt": "tok", "did": "did:plc:abc"}
    blob = {"$type": "blob", "ref": {"$link": "cid"}, "mimeType": "image/jpeg", "size": 9}
    upload = MagicMock(status_code=200)
    upload.json.return_value = {"blob": blob}
    create = MagicMock(status_code=200)
    create.json.return_value = {"uri": "at://post"}
    mock_post.side_effect = [session, upload, create]

    image = PostImage(7, photo, "Fog over a lake", 2000, 1500)
    post_to_bluesky("Hello", {"bluesky_handle": "h", "bluesky_app_password": "p"}, image=image)

    assert mock_post.call_args_list[1].kwargs["data"] == b"jpegbytes"
    embed = mock_post.call_args_list[2].kwargs["json"]["record"]["embed"]
    assert embed["images"][0] == {"alt": "Fog over a lake", "image": blob,
                                  "aspectRatio": {"width": 2000, "height": 1500}}


@patch("bluesky.requests.post")
def test_bluesky_posts_text_only_when_upload_fails(mock_post, tmp_path, capsys):
    photo = tmp_path / "7.jpg"
    photo.write_bytes(b"jpegbytes")
    session = MagicMock(status_code=200)
    session.json.return_value = {"accessJwt": "tok", "did": "did:plc:abc"}
    create = MagicMock(status_code=200)
    create.json.return_value = {"uri": "at://post"}
    mock_post.side_effect = [session, MagicMock(status_code=413, text="too big"), create]

    uri = post_to_bluesky("Hello", {"bluesky_handle": "h", "bluesky_app_password": "p"},
                          image=PostImage(7, photo, "alt", 1, 1))

    assert uri == "at://post"
    assert "embed" not in mock_post.call_args_list[2].kwargs["json"]["record"]
    assert "posting text only" in capsys.readouterr().err


def test_images_dir_is_gitignored():
    assert "images/" in (Path(__file__).parent.parent / ".gitignore").read_text().split()
