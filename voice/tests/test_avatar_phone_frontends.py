"""The arithmetic of the phone's audio front ends (mobile/android/.../PersonalEngine.kt, FeatherFrontend and WenetFrontend), written here step for step in Python and
compared with what the upstream scripts do on the whole voice at once: which samples make which feature, how the ends are padded, how many frames there are and which
features a video frame sees. The encoders are stand-ins that depend only on their own window of samples (the real ones are the person's models); Kotlin itself is not run."""
import numpy as np
import pytest

pytest.importorskip("onnxruntime")
from voice.avatar_engines import phone_export as pe  # noqa: E402

DIM = 1024
FRAMES, MELS, SHIFT, FRAME_LEN = 67, 80, 160, 400


# ---------------------------------------------------------------------------------------------- FeatherTalk
def token_encoder(x: np.ndarray) -> np.ndarray:
    """A stand-in for FeatherHuBERT that is local like its first layers: token k is a function of samples [320 k, 320 k + 400) and of nothing else."""
    count = pe.FEATHER["token_samples"]
    n = max(0, (len(x) - (pe.FEATHER["kernel"] - count)) // count)
    ramp = np.arange(1, DIM + 1, dtype=np.float32) / DIM
    return np.stack([x[k * count:k * count + pe.FEATHER["kernel"]].mean() * ramp for k in range(n)]).astype(np.float32) if n else np.zeros((0, DIM), np.float32)


class FeatherFrontend:
    """FeatherFrontend of PersonalEngine.kt (normalisation left out: the stand-in sees samples as they are)."""

    def __init__(self, block=pe.FEATHER["block_tokens"], context=pe.FEATHER["context_tokens"]):
        self.token, self.kernel, self.per_frame, self.window = pe.FEATHER["token_samples"], pe.FEATHER["kernel"], 2, pe.FEATHER["frames"]
        self.block, self.context, self.overlap = block, context, self.kernel - self.token
        self.wave, self.tokens, self.finished = np.zeros(0, np.float32), [], False

    def available(self):
        n = len(self.wave)
        return 0 if n < self.kernel else (n - self.overlap) // self.token

    def add(self, x):
        self.wave = np.concatenate([self.wave, x])
        self.encode()

    def finish(self):
        self.finished = True
        self.encode()

    def encode(self):
        while True:
            have = self.available()
            up_to = have if self.finished else len(self.tokens) + self.block
            if up_to <= len(self.tokens) or up_to > have:
                return
            done, first = len(self.tokens), max(0, len(self.tokens) - self.context)
            out = token_encoder(self.wave[first * self.token:up_to * self.token + self.overlap])
            assert len(out) == up_to - first
            self.tokens.extend(out[done - first:])

    def ended(self, i):
        return self.finished and i >= len(self.tokens) // self.per_frame

    def feature(self, i):
        half = self.window // 2
        if not self.finished and self.per_frame * (i + half) > len(self.tokens):
            return None
        frames, out = len(self.tokens) // self.per_frame, np.zeros((self.window * self.per_frame, DIM), np.float32)
        for w in range(self.window):
            j = i - half + w
            if 0 <= j < frames:
                for p in range(self.per_frame):
                    out[w * self.per_frame + p] = self.tokens[j * self.per_frame + p]
        return out


def upstream_feather_windows(x: np.ndarray) -> list[np.ndarray]:
    """What data_utils/feather_hubert + inference.py do on the whole voice: all the features at once, an even count, two per frame, then gather_audio_window."""
    hidden = token_encoder(x)
    hidden = hidden[:len(hidden) - len(hidden) % 2].reshape(-1, 2, DIM)
    out = []
    for i in range(len(hidden)):
        left, right = i - 10, i + 10
        window = hidden[max(0, left):min(len(hidden), right)]
        window = np.concatenate([np.zeros((max(0, -left), 2, DIM), np.float32), window, np.zeros((max(0, right - len(hidden)), 2, DIM), np.float32)])
        out.append(window.reshape(-1, DIM))
    return out


@pytest.mark.parametrize("seconds,pieces", [(0.2, [3200]), (1.7, [1000, 5000, 321, 20000]), (4.3, [7777, 100, 16000, 40000]), (9.0, [160, 48000])])
def test_the_feather_windows_are_the_upstream_windows_however_the_voice_arrives(seconds, pieces):
    x = np.random.default_rng(1).standard_normal(int(seconds * 16000)).astype(np.float32)
    want = upstream_feather_windows(x)
    front, i, got, cut = FeatherFrontend(), 0, [], 0
    for size in pieces + [len(x)]:
        front.add(x[cut:cut + size])
        cut += size
        while not front.ended(i) and (w := front.feature(i)) is not None:
            got.append(w)
            i += 1
        if cut >= len(x):
            break
    front.finish()
    while not front.ended(i):
        got.append(front.feature(i))
        i += 1
    assert len(got) == len(want)
    assert all(np.allclose(a, b, atol=1e-6) for a, b in zip(got, want))


def test_a_frame_waits_for_the_voice_it_needs_and_a_short_voice_has_no_frames():
    front = FeatherFrontend()
    front.add(np.ones(8000, np.float32))
    assert front.feature(0) is None, "half a second is 24 features: the first block is 32"
    front.add(np.ones(8000, np.float32))
    assert front.feature(0) is not None, "one second: a block of 32 features is in, and frame 0 needs 20 of them"
    assert front.feature(10) is None, "frame 10 needs the features up to 2 x (10 + 10) = 40"
    short = FeatherFrontend()
    short.add(np.ones(300, np.float32))
    short.finish()
    assert short.ended(0)


# ---------------------------------------------------------------------------------------------- Ultralight with wenet
@pytest.fixture(scope="module")
def fbank(tmp_path_factory):
    import onnxruntime as ort
    path = tmp_path_factory.mktemp("fbank") / "fbank.onnx"
    n = pe.build_fbank_onnx(path)
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return lambda samples: session.run(None, {"samples": samples.astype(np.float32)[None]})[0][0, 0], n


def chunk_encoder(chunk: np.ndarray) -> np.ndarray:
    """A stand-in for the wenet encoder (stateless, as wenet_infer.py uses it): 8192 values that depend on the whole chunk."""
    return (chunk.reshape(-1)[:8192] * 0.01 + chunk.mean()).astype(np.float32) if chunk.size >= 8192 else np.zeros(8192, np.float32)


def upstream_wenet_chunks(x16: np.ndarray) -> list[np.ndarray]:
    """data_utils/wenet_infer.py: 32 x 160 zeros, the voice at 16-bit scale, 35 x 160 zeros, Kaldi's fbank of all of it, chunks of 67 frames every 5."""
    torch = pytest.importorskip("torch")
    kaldi = pytest.importorskip("torchaudio.compliance.kaldi")
    wave = np.concatenate([np.zeros(32 * 160), (x16 * 32767).astype(np.int16).astype(np.float32), np.zeros(35 * 160)]).astype(np.float32)
    feats = kaldi.fbank(torch.from_numpy(wave)[None], num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0, energy_floor=0.0, sample_frequency=16000).numpy()[None]
    out, start, end = [], 0, 0
    while end < feats.shape[1]:
        end = start + 67
        feat = feats[:, start:end, :]
        if feat.shape[1] < 67:
            feat = np.concatenate([feat, np.zeros([1, 67 - feat.shape[1], 80])], axis=1)
        out.append(chunk_encoder(feat.astype(np.float32).reshape(1, 1, 67, 80)))
        start += 5
    return out


class WenetFrontend:
    """WenetFrontend of PersonalEngine.kt, with the filter bank graph in place of the Kotlin call to it."""

    def __init__(self, fbank_run):
        self.run, self.front, self.back, self.step, self.chunk_samples = fbank_run, pe.WENET["front"], pe.WENET["back"], pe.WENET["step"] * SHIFT, (FRAMES - 1) * SHIFT + FRAME_LEN
        self.wave, self.n, self.encoded, self.finished = np.zeros(8, np.float32), pe.WENET["front"], [], False
        self.wave = np.zeros(self.front, np.float32)

    def add(self, x):
        scaled = np.clip(x * 32767.0, -32768.0, 32767.0).astype(np.int32).astype(np.float32)
        self.wave = np.concatenate([self.wave[:self.n], scaled])
        self.n = len(self.wave)
        while self.n >= self.step * len(self.encoded) + self.chunk_samples:
            self.chunk(len(self.encoded))

    def finish(self):
        if self.finished:
            return
        self.wave = np.concatenate([self.wave[:self.n], np.zeros(self.back, np.float32)])
        self.n = len(self.wave)
        self.finished = True
        frames = 0 if self.n < FRAME_LEN else 1 + (self.n - FRAME_LEN) // SHIFT
        step_frames = self.step // SHIFT
        chunks = 1 if frames <= FRAMES else (frames - FRAMES + step_frames - 1) // step_frames + 1
        while len(self.encoded) < chunks:
            self.chunk(len(self.encoded))

    def chunk(self, i):
        start = self.step * i
        samples = np.array([self.wave[start + k] if start + k < self.n else 0.0 for k in range(self.chunk_samples)], np.float32)
        rows = self.run(samples).copy()
        total = 0 if self.n < FRAME_LEN else 1 + (self.n - FRAME_LEN) // SHIFT
        valid = max(0, min(FRAMES, total - (self.step // SHIFT) * i))
        rows[valid:] = 0.0
        self.encoded.append(chunk_encoder(rows.reshape(1, 1, FRAMES, MELS)))


@pytest.mark.parametrize("seconds,pieces", [(0.05, [800]), (0.9, [1600, 4000]), (2.37, [5000, 333, 12345]), (5.0, [16000])])
def test_the_wenet_chunks_are_the_upstream_chunks_however_the_voice_arrives(fbank, seconds, pieces):
    pytest.importorskip("torchaudio")
    run, _ = fbank
    x = (np.random.default_rng(2).standard_normal(int(seconds * 16000)) * 0.1).astype(np.float32)
    want = upstream_wenet_chunks(x)
    front, cut = WenetFrontend(run), 0
    for size in pieces:
        front.add(x[cut:cut + size])
        cut += size
    front.add(x[cut:])
    front.finish()
    assert len(front.encoded) == len(want)
    assert all(np.allclose(a, b, atol=5e-3) for a, b in zip(front.encoded, want))
