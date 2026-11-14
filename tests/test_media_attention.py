"""SageAttention 2 as the attention of the LTX 2.5 clips: the settings, which Python and attention a clip gets (and the plain warning when it falls back to cuDNN), the launcher that sets the attention of the
transformer from outside, the component that installs the wheel, and the clip through the media layer. Stand-ins: no GPU, no wheel is built or loaded."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from media_fake import make_ltx
from test_media import world  # noqa: F401  (the fixture)
from tadamoo.media import attention_env, client, components, ltx_launch, worker
from tadamoo.media.errors import MediaError
from tadamoo.media.settings import DEFAULTS, MediaSettings

STUDIO_PY = "studio/.venv/Scripts/python.exe"


def fake_env(root: Path, package="sageattention", version="2.2.0") -> Path:
    """A folder that looks like a Python environment with a package in it."""
    py = attention_env.python_of(str(root))
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    site = root / "Lib" / "site-packages"
    (site / f"{package}-{version}.dist-info").mkdir(parents=True, exist_ok=True)
    return root


def test_the_defaults_are_sage2_with_teacache_003_and_the_settings_check_their_values(tmp_path):
    assert DEFAULTS["ltx_attention"] == "sage2" and DEFAULTS["ltx_teacache"] == 0.03 and DEFAULTS["ltx_attention_env"] == "" and DEFAULTS["ltx_wheels"] == ""
    s = MediaSettings(tmp_path / "media.json")
    got = s.write({"ltx_attention": "cudnn", "ltx_teacache": 0.05, "ltx_attention_env": str(tmp_path / "env"), "ltx_wheels": str(tmp_path / "w")})
    assert got["ltx_attention"] == "cudnn" and got["ltx_teacache"] == 0.05 and s.read() == got
    assert s.write({"ltx_teacache": 0})["ltx_teacache"] == 0.0                    # off
    for bad in ({"ltx_attention": "sage3"}, {"ltx_teacache": 0.5}, {"ltx_teacache": "x"}, {"ltx_attention_env": 3}):
        with pytest.raises(MediaError):
            s.write(bad)
    (tmp_path / "media.json").write_text('{"ltx_attention": "nope", "ltx_teacache": 9}', encoding="utf-8")
    assert s.read()["ltx_attention"] == "sage2" and s.read()["ltx_teacache"] == 0.03


def test_a_clip_gets_the_sage_environment_when_it_has_the_package_and_cudnn_with_a_plain_warning_when_not(tmp_path):
    base = str(tmp_path / STUDIO_PY)
    env = fake_env(tmp_path / "copy")
    py, how, warning = attention_env.resolve("sage2", base, str(env))
    assert (py, how, warning) == (str(attention_env.python_of(str(env))), "sage2", None)
    assert attention_env.resolve("cudnn", base, str(env)) == (base, "cudnn", None)                   # PyTorch's own: no copy, nothing to say
    assert attention_env.resolve("efficient", base, str(env)) == (base, "efficient", None)
    py, how, warning = attention_env.resolve("sage2", base, str(tmp_path / "nowhere"))
    assert (py, how) == (base, "cudnn") and "SageAttention 2 is not available" in warning and "does not exist" in warning and "cuDNN" in warning and attention_env.HOW in warning
    old = fake_env(tmp_path / "old", version="1.0.6")
    py, how, warning = attention_env.resolve("sage2", base, str(old))
    assert how == "cudnn" and "sageattention 2.* is not installed" in warning
    assert attention_env.resolve("sage1", base, str(old))[1] == "sage1"
    studio = fake_env(tmp_path / "studio" / ".venv")
    py, how, warning = attention_env.resolve("sage2", base, str(studio))
    assert how == "cudnn" and "LTX Studio's own environment" in warning and "never changed" in warning


def test_the_plan_of_a_clip_carries_the_attention_the_teacache_and_the_quality(world, tmp_path):
    env = fake_env(tmp_path / "copy")
    spec = {"ltx_dir": str(world["ltx"]), "video_model": "ltx-2.5-22b-distilled-transformer-nvfp4.safetensors", "output": str(tmp_path / "o.mp4"), "attention": "sage2", "teacache": 0.03, "attention_env": str(env),
            "request": {"prompt": "a presenter", "width": 896, "height": 512, "frames": 169, "fps": 25, "seed": 5, "crf": 4, "crf_preset": "medium"}}
    plan = worker.video_plan(str(world["ltx"]), spec)
    assert plan["python"] == str(attention_env.python_of(str(env))) and plan["script"].endswith("ltx_launch.py") and plan["warnings"] == []
    assert plan["env"]["LTX_ATTENTION"] == "sage2" and plan["env"]["LTX_X264_CRF"] == "4" and plan["params"]["teacache_thresh"] == 0.03
    plain = worker.video_plan(str(world["ltx"]), {**spec, "attention": "cudnn", "teacache": 0, "request": {**spec["request"], "crf": None}})
    assert not plain["script"].endswith("ltx_launch.py") and "LTX_ATTENTION" not in plain["env"] and "teacache_thresh" not in plain["params"]
    fallback = worker.video_plan(str(world["ltx"]), {**spec, "attention_env": str(tmp_path / "nowhere")})
    assert "LTX_ATTENTION" not in fallback["env"] and fallback["python"] != plan["python"] and "SageAttention 2 is not available" in fallback["warnings"][0] and fallback["env"]["LTX_X264_CRF"] == "4"


def test_a_clip_is_made_with_cudnn_and_the_result_says_why_when_sage_is_not_there(world, tmp_path):
    world["settings"].update(ltx_attention="sage2", ltx_attention_env=str(tmp_path / "nowhere"))
    out = tmp_path / "clip" / "o.mp4"
    got = client.run("video", {"prompt": "a presenter speaks", "width": 896, "height": 512, "frames": 169, "fps": 25, "seed": 5, "negative": ""}, out, world["settings"], url=world["broker"].url)
    assert Path(got["path"]).is_file() and len(got["warnings"]) == 1 and "SageAttention 2 is not available" in got["warnings"][0] and "cuDNN" in got["warnings"][0]
    params = json.loads((out.parent / "params.json").read_text(encoding="utf-8"))
    assert params["teacache_thresh"] == 0.03                                                       # the cache is the worker's own parameter and does not depend on the attention
    assert (out.parent / "attention.txt").read_text() == ""
    world["settings"].update(ltx_attention="cudnn", ltx_teacache=0)
    again = client.run("video", {"prompt": "a presenter speaks", "width": 896, "height": 512, "frames": 169, "fps": 25, "seed": 5, "negative": ""}, tmp_path / "clip2" / "o.mp4", world["settings"], url=world["broker"].url)
    assert again["warnings"] == [] and "teacache_thresh" not in json.loads((tmp_path / "clip2" / "params.json").read_text(encoding="utf-8"))


def test_the_component_checks_the_import_and_installs_from_the_wheels_folder(tmp_path, monkeypatch):
    client.forget_checks()
    settings = {**DEFAULTS, "ltx_dir": str(make_ltx(tmp_path / "ltx")), "ltx_attention_env": str(tmp_path / "copy"), "ltx_wheels": str(tmp_path / "wheels")}
    monkeypatch.setattr(components.shutil, "which", lambda name: "/bin/ffmpeg")
    assert "video-attention" in components.SKILLS["video-generation"]
    row = next(r for r in components.check("video-generation", settings) if r["id"] == "video-attention")
    assert not row["present"] and row["where"] == str(tmp_path / "copy") and any("sageattention-2.2.0-*.whl" in m for m in row["missing"])
    todo = [t for t in components.steps("video-generation", settings) if t["component"] == "video-attention"]
    assert len(todo) == 1 and todo[0]["step"][1:] == [attention_env.__file__, "install", "--ltx-dir", str(tmp_path / "ltx"), "--env", str(tmp_path / "copy"), "--wheels", str(tmp_path / "wheels")]
    assert "docs/media/sage-wheel.md" in components.COMPONENTS["video-attention"]["how"][0] and "ltx_wheels" in components.COMPONENTS["video-attention"]["how"][0]
    monkeypatch.setattr(attention_env, "imports", lambda env, package, version="": True)
    assert next(r for r in components.check("video-generation", settings) if r["id"] == "video-attention")["present"]


def test_the_install_copies_the_studio_environment_adds_the_wheels_and_says_what_is_missing(tmp_path, monkeypatch):
    studio = fake_env(tmp_path / "studio" / "venv", package="torch", version="2.14.0")
    monkeypatch.setattr(attention_env, "source_python", lambda ltx_dir: str(attention_env.python_of(str(studio))))
    wheels, env, said, ran = tmp_path / "wheels", tmp_path / "copy", [], []
    wheels.mkdir()
    assert attention_env.install("x", str(env), str(wheels), run=lambda a: ran.append(a) or 0, say=said.append) == 2
    assert "sageattention-2.2.0-*.whl is not in" in said[-1] and "docs/media/sage-wheel.md" in said[-1] and not ran and not env.exists()
    (wheels / "sageattention-2.2.0-cp312-cp312-win_amd64.whl").write_bytes(b"w")
    (wheels / "triton_windows-3.9.0.post30-cp312-abi3-win_amd64.whl").write_bytes(b"w")
    assert attention_env.install("x", str(studio), str(wheels), run=lambda a: ran.append(a) or 0, say=said.append) == 2 and "never" not in said[-1] and "must be another folder" in said[-1]
    assert attention_env.install("x", str(env), str(wheels), run=lambda a: ran.append(a) or 0, say=said.append) == 0
    assert attention_env.python_of(str(env)).is_file() and (studio / "Lib" / "site-packages" / "torch-2.14.0.dist-info").is_dir()              # the copy is made, the studio's own is as it was
    pip, check = ran
    assert pip[1:] == ["-m", "pip", "install", "--no-deps", "--no-index", "--find-links", str(wheels), "sageattention==2.2.0", "triton-windows==3.9.0.post30"]
    assert check[1:3] == ["-I", "-c"] and "sageattention" in check[3] and "SageAttention 2 is installed" in said[-1]
    assert attention_env.install("x", str(env), str(wheels), run=lambda a: 1, say=said.append) == 1 and "pip could not install" in said[-1]
    assert attention_env.install("x", str(env), str(wheels), run=lambda a: 0 if "pip" in a else 1, say=said.append) == 2 and "cannot import sageattention" in said[-1]


LTX_STAND_IN = {
    "ltx_core/__init__.py": "", "ltx_core/model/__init__.py": "", "ltx_core/model/transformer/__init__.py": "",
    "ltx_core/model/transformer/attention.py": "class Attention:\n    attention_function = 'cudnn'\n",
    "ltx_pipelines/__init__.py": "", "ltx_pipelines/utils/__init__.py": "",
    "ltx_pipelines/utils/blocks.py": textwrap.dedent('''
        from ltx_core.model.transformer.attention import Attention
        class Model:
            def __init__(self):
                self.parts = [Attention(), Attention()]
            def modules(self):
                return self.parts
        class DiffusionStage:
            def _build_transformer(self):
                return Model()
        '''),
    "sageattention.py": "def sageattn(q, k, v, tensor_layout='HND'):\n    return q\n",
}
WORKER_STAND_IN = textwrap.dedent('''
    import sys
    stage = None
    def main():
        from ltx_pipelines.utils.blocks import DiffusionStage
        model = DiffusionStage()._build_transformer()
        print("functions:", ",".join(getattr(m.attention_function, "__name__", str(m.attention_function)) for m in model.modules()), flush=True)
        print('save:', {"crf": "19", "preset": "veryfast"}, flush=True)
    if __name__ == "__main__":
        main()
    ''')


def launch(tmp_path, extra_env, files=LTX_STAND_IN, worker_source=WORKER_STAND_IN):
    root = tmp_path / "standin"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    script = root / "workers" / "ltx2_infer.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(worker_source, encoding="utf-8")
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("LTX_")}
    env.update({"PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8", **extra_env})
    return subprocess.run([sys.executable, ltx_launch.__file__, str(script), "p.json"], capture_output=True, text=True, env=env, cwd=str(tmp_path))


def test_the_launcher_sets_the_attention_of_every_attention_module_the_stage_builds(tmp_path):
    done = launch(tmp_path, {"LTX_ATTENTION": "sage2", "LTX_X264_CRF": "4", "LTX_X264_PRESET": "slow"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[attention] sage2 for the attention of the transformer" in done.stdout and "functions: sage,sage" in done.stdout
    assert "'crf': '4', 'preset': 'slow'" in done.stdout                                          # the worker's save setting is replaced in the same run
    plain = launch(tmp_path, {})
    assert "functions: cudnn,cudnn" in plain.stdout and "[attention]" not in plain.stdout and "'crf': '19'" in plain.stdout
    cudnn = launch(tmp_path, {"LTX_ATTENTION": "cudnn"})
    assert "functions: cudnn,cudnn" in cudnn.stdout


def test_a_package_that_cannot_be_imported_is_a_plain_warning_and_the_worker_goes_on_with_cudnn(tmp_path):
    files = {k: v for k, v in LTX_STAND_IN.items() if k != "sageattention.py"}
    done = launch(tmp_path, {"LTX_ATTENTION": "sage2"}, files=files)
    assert done.returncode == 0 and "[attention] WARNING: sage2 could not be used" in done.stdout and "made with cuDNN attention" in done.stdout and "functions: cudnn,cudnn" in done.stdout
    both = launch(tmp_path, {"LTX_ATTENTION": "bogus"})
    assert both.returncode == 0 and "functions: cudnn,cudnn" in both.stdout and "[attention]" not in both.stdout


def test_the_sage_wrapper_gives_the_kernel_heads_in_its_layout_and_returns_the_shape_the_transformer_expects(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    seen = {}

    def sageattn(q, k, v, tensor_layout="HND"):
        seen.update(shape=tuple(q.shape), layout=tensor_layout)
        return q
    monkeypatch.setitem(sys.modules, "sageattention", type(sys)("sageattention"))
    sys.modules["sageattention"].sageattn = sageattn
    call = ltx_launch.make_attention("sage2")
    x = torch.zeros(1, 10, 32 * 128)
    assert tuple(call(x, x, x, 32).shape) == (1, 10, 32 * 128) and seen == {"shape": (1, 32, 10, 128), "layout": "HND"}
