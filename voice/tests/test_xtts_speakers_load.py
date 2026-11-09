"""The XTTS built-in speakers are read from `speakers_xtts.pth` as tensors only: a file of that shape gives the names, and a file that
would run code when unpickled is refused without running it. No model or GPU; torch on the CPU."""
import pickle

import pytest

torch = pytest.importorskip("torch")

from callbridge.engines import catalog  # noqa: E402

RAN = []


class Payload:
    def __reduce__(self):
        return (RAN.append, ("ran",))


@pytest.fixture
def speakers(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_xtts_cache", None)
    monkeypatch.setattr(catalog.models_store, "folder", lambda kind, name, *a, **k: str(tmp_path))
    return tmp_path / "speakers_xtts.pth"


def test_the_names_of_a_dict_of_tensors_are_read(speakers):
    torch.save({"Ana Florence": {"gpt_cond_latent": torch.zeros(1, 32, 4), "speaker_embedding": torch.zeros(1, 8, 1)},
                "Claribel Dervla": {"gpt_cond_latent": torch.zeros(1, 32, 4), "speaker_embedding": torch.zeros(1, 8, 1)}}, speakers)
    assert catalog._xtts_speakers(None) == ["Ana Florence", "Claribel Dervla"]


def test_a_file_that_would_run_code_is_refused_and_does_not_run(speakers):
    with speakers.open("wb") as fh:
        pickle.dump({"x": Payload()}, fh)
    assert catalog._xtts_speakers(None) == []
    assert RAN == []
