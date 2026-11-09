"""What a helper or engine process sees of the service's environment: the system's and the toolkits' variables and the engines' own
settings, never the phone's pairing token, a Hugging Face token or any other token, key, secret or password."""
from callbridge import models_store as ms

SERVICE_ENV = {
    "PATH": "C:\\Windows", "SystemRoot": "C:\\Windows", "TEMP": "C:\\t", "TMP": "C:\\t", "USERPROFILE": "C:\\Users\\me", "HOME": "/home/me",
    "LANG": "it_IT.UTF-8", "LC_ALL": "C.UTF-8", "CUDA_VISIBLE_DEVICES": "1", "CUDA_PATH": "C:\\cuda", "PYTHONPATH": "x", "TORCH_HOME": "t",
    "HF_HOME": "h", "TOKENIZERS_PARALLELISM": "false", "KOKORO_PRECISION": "fp16", "AVATAR_FPS": "25", "OMP_NUM_THREADS": "4",
    "TADAMOO_PHONE_TOKEN": "phone-secret", "HF_TOKEN": "hf_x", "HUGGING_FACE_HUB_TOKEN": "hf_y", "OPENAI_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "a",
    "GITHUB_TOKEN": "g", "MY_PASSWORD": "p", "TADAMOO_AVATAR_ENGINES_PYTHON": "C:\\py\\python.exe", "TADAMOO_DATA": "d", "SOME_TOOL_SETTING": "x",
    "HF_API_KEY": "k", "CUDA_KEY": "k", "TTS_AUTH": "a",
}


def test_the_system_toolkits_and_engine_settings_pass():
    env = ms.child_env(SERVICE_ENV)
    for name in ("PATH", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "HOME", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES", "CUDA_PATH", "PYTHONPATH",
                 "TORCH_HOME", "HF_HOME", "TOKENIZERS_PARALLELISM", "KOKORO_PRECISION", "AVATAR_FPS", "OMP_NUM_THREADS"):
        assert env[name] == SERVICE_ENV[name], name


def test_tokens_keys_secrets_and_other_programs_variables_do_not():
    env = ms.child_env(SERVICE_ENV)
    for name in ("TADAMOO_PHONE_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "MY_PASSWORD",
                 "TADAMOO_AVATAR_ENGINES_PYTHON", "TADAMOO_DATA", "SOME_TOOL_SETTING", "HF_API_KEY", "CUDA_KEY", "TTS_AUTH"):
        assert name not in env, name
    assert "phone-secret" not in repr(env)


def test_helper_env_is_built_on_it_offline_with_the_models_folder(monkeypatch, tmp_path):
    for k, v in SERVICE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(ms.ROOT_ENV, str(tmp_path))
    env = ms.helper_env("tts")
    assert "TADAMOO_PHONE_TOKEN" not in env and "HF_TOKEN" not in env
    assert env[ms.ROOT_ENV] == str(tmp_path) and env["HF_HUB_OFFLINE"] == "1" and env["KOKORO_PRECISION"] == "fp16"
