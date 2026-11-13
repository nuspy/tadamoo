"""Stock footage from Pixabay: the keywords of a clip, the choice among the hits, the cache, the cut, the key's place, and the production that takes its B-roll from it (and shows the credits).
The service is a local stand-in: no call goes to Pixabay."""
import http.server
import json
import shutil
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tadamoo.llm.credentials import FileCredentials
from tadamoo.media import stock
from tadamoo.newscast import produce, script as script_mod
from tadamoo.speech import service as speech
from tadamoo.videoedit import compose
from video_fakes import make_clip
import test_newscast as nc
from test_newscast import stage  # noqa: F401  (the fixture of the production tests)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
KEY = "test-key-0000"


def hit(i, tags, user="alice", duration=9, downloads=100, title="clip"):
    return {"id": i, "pageURL": f"https://pixabay.com/videos/{title}-{i}/", "type": "film", "tags": tags, "duration": duration, "downloads": downloads, "user": user,
            "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0}, "small": {"url": f"FILES/{i}.mp4", "width": 640, "height": 360, "size": 1}}}


class Pixabay:
    """Answers /api/videos/ with the hits `answer(query)` gives and serves one real small video for every file."""

    def __init__(self, answer, status=200, seconds=9.0, folder=None):
        self.requests, self.downloads = [], []
        server = self
        self.video = make_clip(Path(folder) / "stock.mp4", seconds, "640x360")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path.startswith("/files/"):
                    server.downloads.append(parts.path)
                    data = server.video.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                server.requests.append(query)
                body = json.dumps({"total": 1, "totalHits": 1, "hits": [{**h, "videos": {q: {**v, "url": v["url"].replace("FILES", server.url + "/files")} for q, v in h["videos"].items()}}
                                                                         for h in answer(query["q"])]}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.api = self.url + "/api/videos/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def pix(tmp_path):
    made = []

    def make(answer, **kw):
        server = Pixabay(answer, folder=tmp_path, **kw)
        made.append(server)
        return server
    yield make
    for server in made:
        server.stop()


def test_the_keywords_of_a_description_leave_out_what_only_says_how_footage_looks():
    assert stock.keywords("Realistic documentary news footage of a crowded street at night, steady camera") == ["crowded", "street", "night"]
    assert stock.keywords("Buildings and the buildings: a parliament building") == ["buildings", "parliament"]
    assert stock.keywords("the of and") == []


def test_a_hit_is_described_by_its_tags_and_the_title_in_its_address_and_the_match_weighs_the_first_words_more():
    h = stock.Hit(1, "https://pixabay.com/videos/traffic-cars-road-1234/", ["city", "night lights"], "u", 9, 5)
    assert h.words == ["city", "night", "lights", "traffic", "cars", "road"]
    assert stock.match(["cars", "road"], h) == (1.0, 2) and stock.match(["street", "road"], h) == (0.5, 1)
    assert stock.match(["night", "city", "traffic", "bridge"], h)[0] == pytest.approx(3 / 3.6)                     # the fourth keyword weighs 0.6
    assert stock.match([], h) == (0.0, 0)


def test_the_best_matching_video_that_lasts_long_enough_wins_and_one_already_taken_is_not_taken_again(tmp_path, pix):
    hits = [hit(1, "street, night", duration=9, downloads=10), hit(2, "street, night, crowd", duration=9, downloads=5), hit(3, "street, night, crowd", duration=4, downloads=500),
            hit(4, "forest", duration=9)]
    server = pix(lambda q: hits)
    api = stock.Pixabay(KEY, server.api, tmp_path / "cache")
    choice, asked = stock.find(api, [("a crowded street at night", "en")], 6.4, 0.5, set())
    assert choice.hit.id == 2 and choice.match == 1.0 and choice.start == pytest.approx(1.3)               # 2 carries all three keywords ("crowd" for "crowded"); 3 is too short for a clip
    assert asked == ["en: crowded street night"]
    again, _ = stock.find(api, [("a crowded street at night", "en")], 6.4, 0.5, {2})
    assert again.hit.id == 1 and again.match == pytest.approx(2 / 3, abs=0.01)
    none, asked = stock.find(api, [("a spaceship over mars", "en")], 6.4, 0.5, set())
    assert none is None and asked == ["en: spaceship mars", "en: spaceship"]
    assert all(r["key"] == KEY and r["video_type"] == "film" and r["safesearch"] == "true" and r["lang"] == "en" for r in server.requests)
    second, asked = stock.find(api, [("a spaceship", "en"), ("Egy utca éjjel", "hu")], 6.4, 0.5, set())
    assert second is None and asked[-1].startswith("hu:") and server.requests[-1]["lang"] == "hu"


def test_a_search_is_kept_for_a_day_and_the_key_is_not_in_what_is_kept(tmp_path, pix):
    server = pix(lambda q: [hit(1, "street")])
    clock = {"t": 1_000_000.0}
    api = stock.Pixabay(KEY, server.api, tmp_path / "cache", now=lambda: clock["t"])
    api.search("street")
    api.search("street")
    assert len(server.requests) == 1
    import os
    for f in (tmp_path / "cache").iterdir():
        os.utime(f, (clock["t"], clock["t"]))
        assert KEY not in f.read_text(encoding="utf-8")
    clock["t"] += 23 * 3600
    api.search("street")
    assert len(server.requests) == 1
    clock["t"] += 2 * 3600
    api.search("street")
    assert len(server.requests) == 2


def test_pixabay_that_refuses_or_is_not_there_is_said_in_words(tmp_path, pix):
    server = pix(lambda q: [], status=429)
    with pytest.raises(stock.StockError, match="too many requests"):
        stock.Pixabay(KEY, server.api).search("x")
    with pytest.raises(stock.StockError, match="could not be reached"):
        stock.Pixabay(KEY, "http://127.0.0.1:9/api/videos/").search("x")
    with pytest.raises(stock.StockError, match="no Pixabay key"):
        stock.Pixabay("")


def test_the_key_is_in_the_credential_store_and_the_environment_wins(tmp_path):
    keys = stock.Keys(FileCredentials(tmp_path / "c.json"))
    assert keys.get({}) == ""
    keys.set(KEY)
    assert keys.get({}) == KEY and keys.get({"PIXABAY_API_KEY": "from-env"}) == "from-env"
    with pytest.raises(ValueError):
        keys.set("two words")
    keys.set("")
    assert keys.get({}) == ""


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_video_is_downloaded_and_cut_from_its_middle_to_the_size_of_a_clip_without_sound(tmp_path, pix):
    server = pix(lambda q: [hit(7, "street")], seconds=10.0)
    api = stock.Pixabay(KEY, server.api)
    got = api.download(stock.find(api, [("street", "en")], 6.4, 0.5, set())[0].hit, "small", tmp_path / "d" / "7.mp4")
    assert got.is_file() and server.downloads == ["/files/7.mp4"]
    clip = stock.cut(compose.tool("ffmpeg"), got, tmp_path / "clip.mp4", stock.segment_start(10.0, 6.4), 6.4, (1024, 576), 25)
    info = compose.probe(clip)
    assert (info["width"], info["height"]) == (1024, 576) and info["fps"] == pytest.approx(25, abs=0.01) and info["duration"] == pytest.approx(6.4, abs=0.15) and not info["has_audio"]
    assert stock.segment_start(10.0, 6.4) == pytest.approx(1.8) and stock.segment_start(5.0, 6.4) == 0.0


# ---- the production
def one_story_script():
    blocks = [nc.block("fact", "A kormány 4000 milliárd forintot kapott. " + nc.wordy(1)), nc.block("fact", "Ugyanakkor 12 százalék az ellenkező. " + nc.wordy(2), "warm", (2,), "broll",
                                                                                                broll=("a crowded street at night",))]
    return script_mod.parse(nc.good(stories=[nc.story(blocks=blocks)]))


def run(stage, answer, pix, tmp, **kw):
    server = pix(answer)
    work = tmp / "work"
    manifest = produce.make(one_story_script(), nc.settings(stage, width=400, height=266, fps=12, **kw), work,
                            produce.Options(url=stage["server"].url, media=dict(nc.DEFAULTS), swap="per-job", stock_key=KEY, stock_url=server.api))
    return manifest, work, server


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_broll_comes_from_pixabay_when_it_fits_and_the_credits_name_pixabay_and_its_people_for_five_seconds(stage, pix):
    manifest, work, server = run(stage, lambda q: [hit(11, "street, night, crowd", user="Anna Kovacs", title="crowded-street"), hit(12, "street, night", user="Bela Nagy")], pix, stage["tmp"])
    assert "video" not in [k for k, _, _ in stage["jobs"].asked]                                                           # nothing was made by the video model
    broll = next(p for p in manifest["phases"] if p["phase"] == "broll")
    assert broll["stock"] == broll["clips"] >= 1 and broll["made"] == 0
    info = json.loads(next((work / "broll").glob("s1_c1.json")).read_text(encoding="utf-8"))
    assert info["source"] == "pixabay" and info["user"] == "Anna Kovacs" and info["id"] in (11, 12) and KEY not in json.dumps(manifest)
    edit = next(p for p in manifest["phases"] if p["phase"] == "edit")
    spoken = sum(speech.seconds((work / "voice" / f"{i:02d}.wav").read_bytes()) + produce.TAIL_S + produce.GAP_S for i in range(len(one_story_script().blocks())))
    assert edit["body_s"] == pytest.approx(spoken + 5.0, abs=0.01) and edit["duration_s"] == pytest.approx(spoken + 5.0, abs=0.6)
    from tadamoo.videoedit import render
    last = render.decode(Path(manifest["output"]), edit["duration_s"] - 2.0, 0.2, 12)[0]                 # two seconds before the end: on the credits screen
    assert abs(last.mean(axis=(0, 1)) - (9, 18, 40)).max() < 25 and last.std() > 5                        # the card's background, with its type on it
    earlier = render.decode(Path(manifest["output"]), 3.0, 0.2, 12)[0]
    assert abs(earlier.mean(axis=(0, 1)) - (9, 18, 40)).max() > 25


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_what_pixabay_cannot_give_is_made_by_the_video_model_and_without_pixabay_clips_there_are_no_credits(stage, pix):
    manifest, work, server = run(stage, lambda q: [hit(21, "forest, river")], pix, stage["tmp"])
    assert "video" in [k for k, _, _ in stage["jobs"].asked] and any("nothing on Pixabay fits" in n for n in manifest["notes"])
    edit = next(p for p in manifest["phases"] if p["phase"] == "edit")
    spoken = sum(speech.seconds((work / "voice" / f"{i:02d}.wav").read_bytes()) + produce.TAIL_S + produce.GAP_S for i in range(len(one_story_script().blocks())))
    assert edit["body_s"] == pytest.approx(spoken, abs=0.01) and not server.downloads


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_no_key_or_a_setting_that_says_no_means_the_video_model_makes_every_clip(stage, pix):
    server = pix(lambda q: [hit(31, "street, night, crowd")])
    for n, (key, flag) in enumerate((("", True), (KEY, False))):
        stage["jobs"].asked.clear()
        produce.make(one_story_script(), nc.settings(stage, width=400, height=266, fps=12, stock_video=flag), stage["tmp"] / f"w{n}",
                     produce.Options(url=stage["server"].url, media=dict(nc.DEFAULTS), swap="per-job", stock_key=key, stock_url=server.api))
        assert "video" in [k for k, _, _ in stage["jobs"].asked]
    assert server.requests == []


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_pixabay_that_does_not_answer_is_noted_and_the_video_model_takes_over(stage, pix):
    work = stage["tmp"] / "w"
    manifest = produce.make(one_story_script(), nc.settings(stage, width=400, height=266, fps=12), work,
                            produce.Options(url=stage["server"].url, media=dict(nc.DEFAULTS), swap="per-job", stock_key=KEY, stock_url="http://127.0.0.1:9/api/videos/"))
    assert any("Pixabay did not give a clip" in n for n in manifest["notes"]) and "video" in [k for k, _, _ in stage["jobs"].asked]
