"""The avatar routes of the voice service (bearer token, like every route of it).

    GET  /avatar/state                       what the avatar is doing, the personality it follows, why it cannot load, the outputs
    GET  /avatar/list                        the avatars of the store with their size; DELETE /avatar/<name> removes one
    POST /avatar/start {persona?}            load an avatar (the avatar function must be loaded); POST /avatar/stop
    POST /avatar/speak {text, lang?}         say a sentence with the personality's voice; POST /avatar/lipsync, /avatar/interrupt, /avatar/stream
    GET  /avatar/preview.mjpg                the rendered frames as MJPEG (multipart/x-mixed-replace), one stream per watcher
    GET  /avatar/output                      the preview and the virtual camera
    GET  /avatar/engines                     every engine with its availability for the avatar of the personality, and the selection this service runs with
    GET  /avatar/engines/<engine>/setup, POST /avatar/engines/<engine>/check, POST /avatar/engines/python {path}   the Set up panel of a PC engine (setup_routes.py)
    GET  /avatar/enhance                     the acceleration of the video: what this PC can do (and why not), the settings, the plan, the measured cost
    POST /avatar/wizard/start                a draft; POST /avatar/wizard/<draft>/<foto|pose|video|voce> (multipart, field `file`);
    POST /avatar/wizard/<draft>/elabora {name, language?}   make the avatar; GET and DELETE /avatar/wizard/<draft>
    GET, POST (multipart field `file`) and DELETE /avatar/<name>/training-video   the one long video a personalised engine is trained from (avatar_training_video.py)
    POST /avatar/prepare {avatar, clip?}     prepare a clip for MuseTalk (one at a time); GET /avatar/prepare; POST /avatar/prepare/cancel
    POST /avatar/train/plan {avatar, engine, epochs?, asr?, fresh?}   what training a personalised engine (ultralight, feathertalk) would do and need, and a token
    POST /avatar/train/start {token, confirm}   start that plan (one training at a time); GET /avatar/train/status; POST /avatar/train/cancel
    POST /avatar/train/setup {engine, asr?, avatar?, probe?}, POST /avatar/train/python {path}, POST /avatar/train/export {avatar, engine}   the Set up panel
    POST /avatar/visemes {text, duration_s, lang?}   the viseme timeline of a sentence (the phone's "visemes" mode)
    GET  /avatar/<name>/assets.zip           the phone's files: poster, Wav2Lip face and box, the viseme sprites and manifest.json
    GET  /avatar/<name>/engines/<engine>/files/<path>   a file of the manifest's `engines` entry (the phone package of a personalised engine), with Range and ETag
    GET  /avatar/<name>/sprites.png          every mouth sprite over the photo, labelled, in one picture (for the creation page)
    GET  /avatar/<name>/manifest.json        {version, avatar, created, render_modes, files: {path: sha256}}
    GET  /avatar/phone[?persona=slug]        the avatar of a personality and the render modes its files allow (the gateway asks)
    GET  /avatar/models/wav2lip.onnx         the Wav2Lip model the phones run (Range, ETag = SHA-256); 404 with the reason when not installed
    GET  /personas/<name>/poster|mouthbox|wav2lip/box|wav2lip/face, POST /personas/<name>/poster|wav2lip/bocca   (callAPIcall's paths, on the store)
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from flask import Response, jsonify, request, send_file

from callbridge import avatar_assets, avatar_output, avatar_visemes, models_store
from callbridge.avatar_client import PrepareBusy
from callbridge.avatar_store import StoreError, valid_name
from callbridge.sospensione import MSG_SOSPESO, in_sospensione

from voice import persona_link
from voice.avatar_engines import registry as engine_registry, selection as engine_selection, setup_routes as engine_setup
from voice.avatar_service import AvatarService, Unavailable
from voice.avatar_training import TrainingError
from voice.avatar_wizard import STEPS, WizardError

log = logging.getLogger("voice.avatar.routes")
TEXT_MAX = 600
VISEME_TEXT_MAX = 2000
VISEME_DURATION_MAX = 600.0
LANG_RE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


def _err(message: str, code: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def register(app, svc: AvatarService) -> None:
    def follow_settings():
        try:
            svc.sync_camera()
        except Exception as e:                                                   # noqa: BLE001 (a camera that fails must not fail the request)
            log.warning("camera: %s", e)
        try:
            svc.sync_enhance()
        except Exception as e:                                                   # noqa: BLE001 (the acceleration is never needed to answer)
            log.warning("enhance: %s", e)
    app.before_request(follow_settings)

    def known(name):
        try:
            return svc.store.existing(name), None
        except StoreError as e:
            return None, _err(str(e), "not_found", 404)

    # ---- state
    @app.get("/avatar/state")
    def avatar_state():
        client = svc.client()
        alive = svc.loaded()
        active = client._active_persona if alive else None
        preparing = client.preparing
        job = client.prepare_state()
        link = svc.link()
        return jsonify({"loaded": alive, "running": bool(alive and active and not preparing), "persona": active, "preparing": preparing,
                        "progress": client.progress, "error": client.last_error, "problem": client._check_ready_to_start(),
                        "link": link.as_dict(), "prepare": job, "outputs": svc.outputs()})

    @app.get("/avatar/list")
    def avatar_list():
        return jsonify(svc.store.listing() if svc.config.avatars_dir else {"avatars": [], "size_bytes": 0})

    @app.delete("/avatar/<name>")
    def avatar_delete(name):
        client = svc.client()
        folder, bad = known(name)
        if bad:
            return bad
        job = client.prepare_state()
        if client._active_persona == name or client.preparing == name or (job and job["state"] == "running" and job["avatar"] == name):
            return _err("this avatar is in use: stop it first", "in_use", 409)
        if svc.app.config.get("CB_AVATAR_TRAINING") is not None and svc.trainer().busy_with(name):
            return _err("this avatar is being trained: cancel the training first", "in_use", 409)
        freed = svc.store.delete(name)
        return jsonify({"ok": True, "freed_bytes": freed})

    # ---- the worker
    @app.post("/avatar/start")
    def avatar_start():
        client = svc.client()
        if in_sospensione():
            return _err(MSG_SOSPESO, "suspended", 409)
        if not svc.loaded():
            return _err("the avatar function is not loaded: load it from Models > Voice services", "not_loaded", 409)
        link = svc.link()
        name = _body().get("persona") or link.avatar
        if not name or not valid_name(name):
            return _err(link.problem or "choose an avatar for the personality in Settings > Personalities", "no_avatar", 409)
        _folder, bad = known(name)
        if bad:
            return bad
        if client.is_ready(name) and not client.preparing:
            return jsonify({"status": "ok", "persona": name})
        if not client.start_async(name):
            return _err(f"{client.preparing or 'another avatar'} is being prepared", "busy", 409)
        return jsonify({"status": "preparing", "persona": name}), 202

    @app.post("/avatar/stop")
    def avatar_stop():
        client = svc.client()
        if svc.loaded():
            client._active_persona = None
            client._persona_ready.clear()
        return jsonify({"status": "ok"})

    @app.post("/avatar/speak")
    def avatar_speak():
        body = _body()
        text, lang = body.get("text"), body.get("lang", "")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
            return _err(f"send a sentence of at most {TEXT_MAX} characters", "bad_request", 400)
        if not isinstance(lang, str) or (lang and not LANG_RE.fullmatch(lang)):
            return _err("lang is a language code such as it or en", "bad_request", 400)
        try:
            return jsonify(svc.speak(text.strip(), lang))
        except Unavailable as e:
            return _err(str(e), e.code, e.status)
        except models_store.ModelNotInstalled as e:
            return _err(str(e), "model_not_installed", 409)
        except Exception as e:                                                   # noqa: BLE001 (an engine that failed, no output)
            log.warning("avatar speech: %s", e)
            return _err(f"{e.__class__.__name__}: {e}", "speak_failed", 502)

    @app.post("/avatar/lipsync")
    def avatar_lipsync():
        client = svc.client()
        if not client.is_ready():
            return _err("the avatar is not loaded with an avatar: POST /avatar/start first", "not_loaded", 409)
        body = _body()
        wav = body.get("wav")
        if not isinstance(wav, str) or not wav.strip():
            return _err("send the wav file to animate", "bad_request", 400)
        if not Path(wav).is_file():
            return _err("that wav file does not exist", "bad_request", 400)
        client.speak_wav(wav, mute=bool(body.get("mute", True)))
        started = True
        if body.get("attendi_avvio", True):
            try:
                started = client.wait_until_playing(min(60.0, float(body.get("attesa_max_s", 15.0))))
            except (TypeError, ValueError):
                return _err("attesa_max_s is a number of seconds", "bad_request", 400)
        return jsonify({"status": "ok", "partito": started, "preparazione_s": client.preparazione_s})

    @app.post("/avatar/interrupt")
    def avatar_interrupt():
        svc.client().interrupt(svuota=bool(_body().get("svuota")))
        return jsonify({"status": "ok"})

    @app.post("/avatar/stream")
    def avatar_stream():
        body = _body()
        try:
            port = int(body.get("port") or 0)
        except (TypeError, ValueError):
            return _err("port is a number", "bad_request", 400)
        if not 0 <= port < 65536:
            return _err("port is a number", "bad_request", 400)
        on = bool(body.get("on", True))
        if not svc.client().set_stream(on, port):
            return _err("the avatar worker is not running", "not_loaded", 409)
        return jsonify({"status": "ok", "streaming": on})

    # ---- outputs
    @app.get("/avatar/output")
    def avatar_output_state():
        return jsonify(svc.outputs())

    @app.get("/avatar/enhance")
    def avatar_enhance_state():
        return jsonify(svc.enhance_state())

    @app.get("/avatar/engines")
    def avatar_engines():
        link = svc.link()
        folder = svc.store.path(link.avatar) if link.avatar else None
        return jsonify({"avatar": link.avatar, "engines": engine_registry.describe(folder if folder and folder.is_dir() else None),
                        "selection": engine_selection.parse({"engines": svc.config.avatar_engines, "resolution": svc.config.avatar_resolution,
                                                              "fps_hint": svc.config.avatar_fps_hint}).as_dict()})

    engine_setup.register(app)                                                   # GET /avatar/engines/<engine>/setup, POST .../check, POST /avatar/engines/python

    @app.get("/avatar/preview.mjpg")
    def avatar_preview():
        svc.client()
        if not svc.loaded():
            return _err("the avatar is not loaded", "not_loaded", 409)
        if not svc.preview.open():
            return _err("too many watchers", "busy", 503)

        def stream():
            try:
                yield from svc.preview.stream()
            finally:
                svc.preview.close()
        return Response(stream(), mimetype=avatar_output.CONTENT_TYPE, headers={"Cache-Control": "no-store"})

    # ---- the wizard
    def wizard():
        return svc.wizard()

    def wizard_failed(e: WizardError):
        return _err(str(e), e.code, e.status)

    @app.post("/avatar/wizard/start")
    def wizard_start():
        try:
            return jsonify(wizard().start())
        except (WizardError, Unavailable) as e:
            return _err(str(e), e.code, e.status)

    @app.get("/avatar/wizard/<draft_id>")
    def wizard_status(draft_id):
        try:
            return jsonify(wizard().status(draft_id))
        except (WizardError, Unavailable) as e:
            return _err(str(e), e.code, e.status)

    @app.post("/avatar/wizard/<draft_id>/<step>")
    def wizard_upload(draft_id, step):
        if step == "elabora":
            return wizard_finish(draft_id)
        if step not in STEPS:
            return _err(f"unknown step: {step}", "bad_request", 400)
        sent = request.files.get("file") or request.files.get(step)
        if sent is None:
            return _err("send the file as a multipart field called file", "bad_request", 400)
        try:
            return jsonify(wizard().upload(draft_id, step, sent, request.content_length))
        except (WizardError, Unavailable) as e:
            return _err(str(e), e.code, e.status)

    def wizard_finish(draft_id):
        body = _body()
        language = body.get("language", "it")
        if not isinstance(language, str) or not LANG_RE.fullmatch(language):
            return _err("language is a language code such as it or en", "bad_request", 400)
        try:
            out = wizard().finish(draft_id, body.get("name"), language)
        except (WizardError, Unavailable) as e:
            return _err(str(e), e.code, e.status)
        out["can_prepare"] = svc.loaded() and bool(out["clips"])
        return jsonify(out)

    @app.delete("/avatar/wizard/<draft_id>")
    def wizard_cancel(draft_id):
        try:
            wizard().cancel(draft_id)
        except (WizardError, Unavailable) as e:
            return _err(str(e), e.code, e.status)
        return jsonify({"ok": True})

    # ---- preparing a clip
    @app.post("/avatar/prepare")
    def prepare_start():
        body = _body()
        name, clip = body.get("avatar"), body.get("clip")
        client = svc.client()
        try:
            if clip is None:
                clips = svc.store.clips(name) if valid_name(name) else []
                if len(clips) != 1:
                    return _err("name the clip to prepare", "bad_request", 400)
                clip = clips[0]
            return jsonify(client.prepare(name, clip)), 202
        except StoreError as e:
            return _err(str(e), "not_found", 404)
        except PrepareBusy as e:
            return _err(str(e), "busy", 409)
        except RuntimeError as e:
            return _err(str(e), "not_loaded", 409)

    @app.get("/avatar/prepare")
    def prepare_state():
        return jsonify({"job": svc.client().prepare_state()})

    @app.post("/avatar/prepare/cancel")
    def prepare_cancel():
        return jsonify({"cancelling": svc.client().cancel_prepare()})

    # ---- training a personalised engine
    def training(call):
        try:
            return jsonify(call(svc.trainer()))
        except TrainingError as e:
            return jsonify({"error": str(e), "code": e.code, **e.extra}), e.status
        except Unavailable as e:
            return _err(str(e), e.code, e.status)

    @app.post("/avatar/train/plan")
    def train_plan():
        body = _body()
        return training(lambda jobs: jobs.plan(body.get("avatar"), body.get("engine"), body.get("epochs"), body.get("asr"), body.get("fresh")))

    @app.post("/avatar/train/start")
    def train_start():
        body = _body()
        return training(lambda jobs: jobs.start(body.get("token"), body.get("confirm")))

    @app.post("/avatar/train/setup")
    def train_setup():
        body = _body()
        return training(lambda jobs: jobs.setup(body.get("engine"), body.get("asr"), body.get("avatar"), body.get("probe") is True))

    @app.post("/avatar/train/python")
    def train_python():
        return training(lambda jobs: jobs.set_python(_body().get("path")))

    @app.post("/avatar/train/export")
    def train_export():
        body = _body()
        return training(lambda jobs: jobs.export(body.get("avatar"), body.get("engine")))

    @app.get("/avatar/train/status")
    def train_status():
        return training(lambda jobs: jobs.status())

    @app.post("/avatar/train/cancel")
    def train_cancel():
        return training(lambda jobs: jobs.cancel())

    # ---- the training video
    def training_video(call):
        try:
            return jsonify(call(svc.training_videos()))
        except TrainingError as e:
            return jsonify({"error": str(e), "code": e.code, **e.extra}), e.status
        except Unavailable as e:
            return _err(str(e), e.code, e.status)

    def training_busy(name):
        return svc.app.config.get("CB_AVATAR_TRAINING") is not None and svc.trainer().busy_with(name)

    @app.get("/avatar/<name>/training-video")
    def training_video_info(name):
        return training_video(lambda videos: videos.info(name))

    @app.post("/avatar/<name>/training-video")
    def training_video_save(name):
        sent = request.files.get("file")
        if sent is None:
            return _err("send the video as a multipart field called file", "bad_request", 400)
        if training_busy(name):
            return _err("this avatar is being trained: cancel the training first", "in_use", 409)
        return training_video(lambda videos: videos.save(name, sent, request.content_length))

    @app.delete("/avatar/<name>/training-video")
    def training_video_remove(name):
        if training_busy(name):
            return _err("this avatar is being trained: cancel the training first", "in_use", 409)
        return training_video(lambda videos: videos.remove(name))

    # ---- visemes and the phone's assets
    @app.post("/avatar/visemes")
    def visemes():
        from callbridge import viseme_gen
        body = _body()
        text, seconds, lang = body.get("text"), body.get("duration_s"), body.get("lang", "it")
        if not isinstance(text, str) or not text.strip() or len(text) > VISEME_TEXT_MAX:
            return _err(f"send a text of at most {VISEME_TEXT_MAX} characters", "bad_request", 400)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= VISEME_DURATION_MAX:
            return _err(f"duration_s is the length of the audio in seconds, up to {VISEME_DURATION_MAX:.0f}", "bad_request", 400)
        if not isinstance(lang, str) or not LANG_RE.fullmatch(lang):
            return _err("lang is a language code such as it or en", "bad_request", 400)
        return jsonify(viseme_gen.timeline(text.strip(), float(seconds), lang))

    def wav2lip_ready(name: str, folder: Path) -> bool:
        """The files a phone needs, made from the photo when the folder lacks them; False when there is no usable photo."""
        if avatar_assets.render_modes(folder) == ["wav2lip", "visemes"]:
            return True
        if svc.poster(name) is None:
            return False
        from callbridge import avatar_prep
        try:
            avatar_prep.completa(folder)
            return True
        except (ValueError, OSError, KeyError) as e:
            log.warning("phone assets of %s not made: %s", name, e)
            return False

    def manifest_of(name: str, folder: Path) -> dict:
        store = svc.store
        video = bool(set(store.clips(name)) & set(store.prepared(name)))
        return avatar_assets.manifest(folder, name, str(store.metadata(name).get("created") or ""), video)

    @app.get("/avatar/<name>/manifest.json")
    def avatar_manifest(name):
        folder, bad = known(name)
        if bad:
            return bad
        if not wav2lip_ready(name, folder):
            return _err("this avatar has no photo to make the phone's assets from", "no_poster", 404)
        return jsonify(manifest_of(name, folder))

    @app.get("/avatar/<name>/assets.zip")
    def avatar_assets_zip(name):
        folder, bad = known(name)
        if bad:
            return bad
        if not wav2lip_ready(name, folder):
            return _err("this avatar has no photo to make the phone's assets from", "no_poster", 404)
        data = avatar_assets.zip_bytes(folder, manifest_of(name, folder))
        return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=f"{name}-avatar.zip")

    @app.get("/avatar/<name>/engines/<engine>/files/<path:rel>")
    def avatar_engine_file(name, engine, rel):
        """A file an engine exported for a phone (phone.json, a model, the template frame): only one the avatar's manifest lists for that engine, with Range and ETag."""
        folder, bad = known(name)
        if bad:
            return bad
        entry = avatar_assets.engine_entries(folder).get(engine)
        path = f"{avatar_assets.ENGINES_DIR}/{engine}/export/{rel}"
        if entry is None or path not in entry["files"]:
            return _err("no such file", "not_found", 404)
        digest = entry["files"][path]
        resp = send_file(folder / path, mimetype="application/octet-stream", conditional=True, etag=digest, max_age=0)
        resp.headers["Cache-Control"] = "private, no-cache"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-Content-SHA256"] = digest
        return resp

    @app.get("/avatar/<name>/sprites.png")
    def avatar_sprites(name):
        folder, bad = known(name)
        if bad:
            return bad
        if not wav2lip_ready(name, folder):
            return _err("this avatar has no photo to make mouth shapes from", "no_poster", 404)
        data = avatar_visemes.sheet_png(folder)
        if data is None:
            return _err("this avatar has no mouth sprites", "not_found", 404)
        return Response(data, mimetype="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/avatar/phone")
    def avatar_phone():
        """The avatar a personality shows and the render modes its files allow."""
        slug = request.args.get("persona", "")
        if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", slug):
            return _err("persona is the slug of a personality", "bad_request", 400)
        link = persona_link.resolve(slug, svc._api(), svc.store) if slug else svc.link()
        modes: list[str] = []
        if link.avatar:
            folder = svc.store.existing(link.avatar)
            if wav2lip_ready(link.avatar, folder):
                modes = manifest_of(link.avatar, folder)["render_modes"]
        return jsonify({"persona": link.slug, "avatar": link.avatar, "render_modes": modes, "problem": link.problem})

    @app.get("/avatar/models/wav2lip.onnx")
    def avatar_model():
        path = avatar_assets.model_path()
        if path is None:
            return _err(models_store.reason(avatar_assets.MODEL_ID) or "the Wav2Lip model is not installed on this PC", "model_not_installed", 404)
        digest = avatar_assets.sha256_of(path)
        resp = send_file(path, mimetype="application/octet-stream", conditional=True, etag=digest, max_age=0)
        resp.headers["Cache-Control"] = "private, no-cache"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-Content-SHA256"] = digest
        return resp

    # ---- callAPIcall's paths for the images, on the store
    def image(name, filename, mimetype):
        folder, bad = known(name)
        if bad:
            return bad
        wav2lip_ready(name, folder)
        path = svc.poster(name) if filename == "poster.jpg" else folder / "wav2lip" / filename
        if path is None or not path.is_file():
            return _err("no such asset", "not_found", 404)
        resp = send_file(path, mimetype=mimetype, conditional=True)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/personas/<name>/poster")
    def persona_poster(name):
        return image(name, "poster.jpg", "image/jpeg")

    @app.get("/personas/<name>/wav2lip/face")
    def persona_face(name):
        return image(name, "face.png", "image/png")

    @app.get("/personas/<name>/wav2lip/box")
    def persona_box(name):
        return image(name, "box.json", "application/json")

    @app.get("/personas/<name>/mouthbox")
    def persona_mouthbox(name):
        folder, bad = known(name)
        if bad:
            return bad
        if not wav2lip_ready(name, folder):
            return _err("no such asset", "not_found", 404)
        box = json.loads((folder / "wav2lip" / "box.json").read_text(encoding="utf-8"))
        pw, ph = box.get("poster_w") or 1, box.get("poster_h") or 1
        corners = box.get("bocca") or {}
        if corners.get("p1") and corners.get("p2"):
            import math
            p1, p2 = corners["p1"], corners["p2"]
            return jsonify({"cx": (p1[0] + p2[0]) / 2 / pw, "cy": (p1[1] + p2[1]) / 2 / ph, "w": math.hypot(p2[0] - p1[0], p2[1] - p1[1]) * 1.3 / pw})
        return jsonify({"cx": (box["x"] + box["w"] * 0.5) / pw, "cy": (box["y"] + box["h"] * 0.78) / ph, "w": (box["w"] * 0.42) / pw})

    @app.post("/personas/<name>/poster")
    def persona_set_poster(name):
        folder, bad = known(name)
        if bad:
            return bad
        sent = request.files.get("file")
        if sent is None:
            return _err("send the photo as a multipart field called file", "bad_request", 400)
        import cv2
        from callbridge import avatar_prep
        from voice.avatar_wizard import LIMITS, read_image
        data = sent.stream.read(LIMITS["foto"] + 1)
        if len(data) > LIMITS["foto"]:
            return _err("the photo is too big", "too_big", 413)
        try:
            img = read_image(data)
        except WizardError as e:
            return _err(str(e), e.code, e.status)
        cv2.imwrite(str(folder / "poster.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        try:
            avatar_prep.prepara_wav2lip(folder / "poster.jpg", folder)
        except (ValueError, OSError) as e:
            log.warning("mouth of %s not placed: %s", name, e)
        return jsonify({"ok": True})

    @app.post("/personas/<name>/wav2lip/bocca")
    def persona_set_mouth(name):
        folder, bad = known(name)
        if bad:
            return bad
        body = _body()
        p1, p2 = body.get("p1"), body.get("p2")
        if not all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p) for p in (p1, p2)):
            return _err("send p1 and p2 as [x, y]", "bad_request", 400)
        poster = svc.poster(name)
        if poster is None:
            return _err("this avatar has no poster", "no_poster", 404)
        from callbridge import avatar_prep
        try:
            result = avatar_prep.prepara_da_bocca(poster, folder, p1, p2)
        except (ValueError, OSError) as e:
            return _err(str(e), "bad_request", 400)
        return jsonify({"ok": True, "box": result["box"]})
