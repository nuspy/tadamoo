import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/services.dart';

import 'avatar_assets.dart';
import 'avatar_devices.dart';
import 'avatar_renderer.dart';
import 'viseme_timeline.dart';

/// The slowest speed, in frames a second, at which Wav2Lip is worth showing. The self-test must reach it for the mode to be declared.
const wav2lipMinFps = 12.0;

/// How far behind the voice the picture may fall, and for how long, before the mode is given up.
const wav2lipLagLimitMs = 600;
const wav2lipLagHoldMs = 1500;

/// Android's thermal status from which the phone is too hot to go on drawing (THERMAL_STATUS_SEVERE).
const thermalLimit = 3;

/// One frame the model drew: the 96 x 96 face, as RGBA, for the instant `tMs` of the reply (milliseconds from its first sample).
class Wav2LipFrame {
  Wav2LipFrame(this.gen, this.tMs, this.w, this.h, this.rgba);
  final int gen;
  final int tMs;
  final int w;
  final int h;
  final Uint8List rgba;
}

abstract class Wav2LipEvent {}

class Wav2LipFrameEvent extends Wav2LipEvent {
  Wav2LipFrameEvent(this.frame);
  final Wav2LipFrame frame;
}

class Wav2LipThermalEvent extends Wav2LipEvent {
  Wav2LipThermalEvent(this.level);
  final int level;
}

class Wav2LipErrorEvent extends Wav2LipEvent {
  Wav2LipErrorEvent(this.why);
  final String why;
}

class Wav2LipLoad {
  const Wav2LipLoad(this.ok, [this.reason = '', this.detail = '']);
  final bool ok;

  /// `no_runtime`, `no_model`, `onnx_load_failed`, `bad_io`, `no_face`, ...
  final String reason;
  final String detail;
}

class Wav2LipStep {
  const Wav2LipStep(this.name, this.ok, this.detail);
  final String name;
  final bool ok;
  final String detail;
}

/// The self-test and the benchmark of the model on this phone (Wav2LipBench).
class Wav2LipTestResult {
  const Wav2LipTestResult({required this.ok, this.reason = '', this.steps = const [], this.msPerFrame = 0, this.fps = 0, this.threads = 0, this.frames = 0, this.requiredFps = wav2lipMinFps});

  final bool ok;
  final String reason;
  final List<Wav2LipStep> steps;
  final double msPerFrame;
  final double fps;
  final int threads;
  final int frames;
  final double requiredFps;

  factory Wav2LipTestResult.fromMap(Map<Object?, Object?> m) {
    final steps = [
      for (final s in (m['steps'] as List? ?? const []).whereType<Map>()) Wav2LipStep('${s['name']}', s['ok'] == true, '${s['detail'] ?? ''}'),
    ];
    final ms = (m['msPerFrame'] as num?)?.toDouble() ?? 0;
    final fps = (m['fps'] as num?)?.toDouble() ?? (ms > 0 ? 1000 / ms : 0);
    final functional = m['ok'] == true;
    final reason = functional && fps < wav2lipMinFps ? 'too_slow' : '${m['reason'] ?? ''}';
    return Wav2LipTestResult(
      ok: functional && fps >= wav2lipMinFps,
      reason: reason,
      steps: steps,
      msPerFrame: ms,
      fps: fps,
      threads: (m['threads'] as num?)?.toInt() ?? 0,
      frames: (m['frames'] as num?)?.toInt() ?? 0,
    );
  }

  /// The report as plain English text to paste into a message.
  String toText() {
    final lines = <String>[
      'TaDaMoo avatar test (Wav2Lip on this phone)',
      'Result: ${ok ? 'passed' : 'failed${reason.isEmpty ? '' : ' ($reason)'}'}',
      for (final s in steps) '${s.ok ? 'ok  ' : 'FAIL'} ${s.name}${s.detail.isEmpty ? '' : ': ${s.detail}'}',
      if (msPerFrame > 0) 'Wav2LipBench: ${msPerFrame.toStringAsFixed(1)} ms per frame, ${fps.toStringAsFixed(1)} frames/s over $frames frames, $threads threads',
      'Needed: at least ${requiredFps.toStringAsFixed(0)} frames/s',
      'The test checks that the model runs, that its output resembles its input and how fast it is. It cannot tell whether the lips look right: look at the avatar.',
    ];
    return lines.join('\n');
  }
}

/// The model's runtime on the phone. The real one is Kotlin (Wav2LipChannel.kt, ONNX Runtime); the tests use a stand-in.
abstract class Wav2LipBackend {
  /// Whether this build has the runtime at all.
  Future<bool> available();

  /// Opens the model file and the 96 x 96 face (`wav2lip/face.png`).
  Future<Wav2LipLoad> load({required String model, required String face});

  /// Runs the checks and the benchmark. `face` is the avatar's face when there is one, else the test makes its own.
  Future<Wav2LipTestResult> selfTest({String? model, String? face, required int fps});

  /// Starts a reply at `sampleRate`; the number it returns names its frames. The voice follows through `feed`.
  Future<int> start(int sampleRate, int fps);
  void feed(Uint8List pcm16);

  /// No more voice in this reply: the rest of its frames are drawn.
  Future<void> finish();

  /// Drops what is queued and stops drawing.
  Future<void> stop();

  Future<void> unload();

  /// The frames, the phone's thermal status and the failures of the runtime.
  Stream<Wav2LipEvent> get events;
}

/// [Wav2LipBackend] over the Kotlin channels `app.basalt/wav2lip` (calls) and `app.basalt/wav2lip/events` (frames).
class ChannelWav2LipBackend implements Wav2LipBackend {
  ChannelWav2LipBackend({MethodChannel? channel, EventChannel? events})
      : _channel = channel ?? const MethodChannel('app.basalt/wav2lip'),
        _events = events ?? const EventChannel('app.basalt/wav2lip/events');

  final MethodChannel _channel;
  final EventChannel _events;

  Future<T?> _call<T>(String method, [Object? args]) async {
    try {
      return await _channel.invokeMethod<T>(method, args);
    } on MissingPluginException {
      return null;
    }
  }

  @override
  Future<bool> available() async => await _call<bool>('available') ?? false;

  @override
  Future<Wav2LipLoad> load({required String model, required String face}) async {
    try {
      final m = await _call<Map<Object?, Object?>>('load', {'model': model, 'face': face});
      if (m == null) return const Wav2LipLoad(false, 'no_runtime');
      return Wav2LipLoad(m['ok'] == true, '${m['reason'] ?? ''}', '${m['detail'] ?? ''}');
    } on PlatformException catch (e) {
      return Wav2LipLoad(false, 'onnx_load_failed', '${e.message}');
    }
  }

  @override
  Future<Wav2LipTestResult> selfTest({String? model, String? face, required int fps}) async {
    try {
      final m = await _call<Map<Object?, Object?>>('selfTest', {'model': model, 'face': face, 'fps': fps});
      if (m == null) return const Wav2LipTestResult(ok: false, reason: 'no_runtime');
      return Wav2LipTestResult.fromMap(m);
    } on PlatformException catch (e) {
      return Wav2LipTestResult(ok: false, reason: e.message ?? e.code);
    }
  }

  @override
  Future<int> start(int sampleRate, int fps) async => await _call<int>('start', {'rate': sampleRate, 'fps': fps}) ?? 0;

  @override
  void feed(Uint8List pcm16) {
    _call<void>('feed', pcm16).catchError((Object _) {});
  }

  @override
  Future<void> finish() async {
    await _call<void>('finish');
  }

  @override
  Future<void> stop() async {
    await _call<void>('stop');
  }

  @override
  Future<void> unload() async {
    await _call<void>('unload');
  }

  @override
  Stream<Wav2LipEvent> get events => _events.receiveBroadcastStream().map<Wav2LipEvent?>((e) {
        if (e is! Map) return null;
        switch (e['k']) {
          case 'frame':
            final rgba = e['rgba'];
            if (rgba is! Uint8List) return null;
            return Wav2LipFrameEvent(Wav2LipFrame((e['g'] as num?)?.toInt() ?? 0, (e['t'] as num?)?.toInt() ?? 0, (e['w'] as num?)?.toInt() ?? 96, (e['h'] as num?)?.toInt() ?? 96, rgba));
          case 'thermal':
            return Wav2LipThermalEvent((e['level'] as num?)?.toInt() ?? 0);
          case 'error':
            return Wav2LipErrorEvent('${e['why'] ?? 'inference_failed'}');
        }
        return null;
      }).where((e) => e != null).cast<Wav2LipEvent>();
}

/// The files of the Wav2Lip mode, decoded: the photo, and where the 96 x 96 face and its mouth sit on it.
class Wav2LipPack {
  Wav2LipPack({required this.poster, required this.posterSize, required this.inv, required this.mouth, required this.facePath});

  final ui.Image poster;
  final Size posterSize;

  /// `box.json` `inv`: from the 96 x 96 face to the photo's pixels.
  final List<double> inv;

  /// The part of the face that is drawn over the photo, in the face's own pixels.
  final Rect mouth;
  final String facePath;

  static Future<Wav2LipPack?> load(AvatarAssetStore store, String name, {Future<ui.Image> Function(Uint8List)? decoder}) async {
    try {
      final box = jsonDecode(await File(store.pathOf(name, 'wav2lip/box.json')).readAsString());
      if (box is! Map || box['inv'] is! List || box['mouth'] is! Map) return null;
      final inv = [for (final v in box['inv'] as List) (v as num).toDouble()];
      final m = box['mouth'] as Map;
      if (inv.length != 6) return null;
      final mouth = Rect.fromLTWH((m['x'] as num).toDouble(), (m['y'] as num).toDouble(), (m['w'] as num).toDouble(), (m['h'] as num).toDouble());
      if (mouth.width <= 0 || mouth.height <= 0) return null;
      final poster = await (decoder ?? VisemePack.decode)(await File(store.pathOf(name, 'poster.jpg')).readAsBytes());
      final face = File(store.pathOf(name, 'wav2lip/face.png'));
      if (!face.existsSync()) return null;
      final pw = (box['poster_w'] as num?)?.toDouble() ?? poster.width.toDouble();
      final ph = (box['poster_h'] as num?)?.toDouble() ?? poster.height.toDouble();
      return Wav2LipPack(poster: poster, posterSize: Size(pw, ph), inv: inv, mouth: mouth, facePath: face.path);
    } catch (_) {
      return null;
    }
  }
}

Future<ui.Image> _fromRgba(Wav2LipFrame f) {
  final done = Completer<ui.Image>();
  ui.decodeImageFromPixels(f.rgba, f.w, f.h, ui.PixelFormat.rgba8888, done.complete);
  return done.future;
}

class _Shown {
  _Shown(this.at, this.image);
  final int at;
  final ui.Image image;
}

/// The lips the model draws from the voice, laid over the photo at the time of the audio each frame was made for. It gives the mode up
/// (`onFailed`) when the model falls behind the voice for too long, when the phone gets too hot, or when the runtime fails.
class Wav2LipRenderer extends AvatarRenderer {
  Wav2LipRenderer({
    required this.backend,
    required this.pack,
    required this.player,
    this.fps = 25,
    this.leadMs = 30,
    int Function()? now,
    Future<ui.Image> Function(Wav2LipFrame)? toImage,
  })  : _now = now ?? _wall,
        _toImage = toImage ?? _fromRgba {
    _sub = backend.events.listen(_onEvent);
  }

  static final Stopwatch _clock = Stopwatch()..start();
  static int _wall() => _clock.elapsedMilliseconds;

  final Wav2LipBackend backend;
  final Wav2LipPack pack;
  final PcmPlayer player;
  final int fps;
  final int leadMs;
  final int Function() _now;
  final Future<ui.Image> Function(Wav2LipFrame) _toImage;

  StreamSubscription<Wav2LipEvent>? _sub;
  final Map<int, int> _bases = {};
  final Queue<_Shown> _queue = Queue();
  int? _pendingBase;
  bool _open = false;
  bool _failed = false;
  int _latestAt = 0;
  int _lagFrom = -1;
  int _framesShown = 0;
  ui.Image? _current;
  (double, double, double, double) motion = (0, 0, 0, 1);

  @override
  String get mode => 'wav2lip';

  /// How many frames reached the screen, for the tests and the tab's note.
  int get framesShown => _framesShown;

  @override
  void audioStart(int sampleRate) {
    if (_open || _failed) return;
    _open = true;
    final base = player.queuedMs;
    _pendingBase = base;
    _latestAt = base - 1;
    _lagFrom = -1;
    backend.start(sampleRate, fps).then((gen) {
      _bases[gen] = _pendingBase ?? base;
    });
  }

  @override
  void audio(Uint8List pcm16) {
    if (_open && !_failed) backend.feed(pcm16);
  }

  @override
  void audioEnd() {
    if (!_open) return;
    _open = false;
    backend.finish();
  }

  @override
  void flush() {
    _open = false;
    backend.stop();
    _drop();
    _bases.clear();
    _pendingBase = null;
    _current = null;
    _lagFrom = -1;
    notifyListeners();
  }

  void _drop() {
    for (final s in _queue) {
      s.image.dispose();
    }
    _queue.clear();
  }

  void _onEvent(Wav2LipEvent e) {
    if (_failed) return;
    if (e is Wav2LipFrameEvent) {
      final base = _bases[e.frame.gen] ?? _pendingBase;
      if (base == null) return;
      final at = base + e.frame.tMs;
      if (at > _latestAt) _latestAt = at;
      _toImage(e.frame).then((img) {
        if (_failed || (_bases[e.frame.gen] == null && _pendingBase == null)) {
          img.dispose();
          return;
        }
        _queue.addLast(_Shown(at, img));
        while (_queue.length > 64) {
          _queue.removeFirst().image.dispose();
        }
      });
    } else if (e is Wav2LipThermalEvent) {
      if (e.level >= thermalLimit) _giveUp('thermal');
    } else if (e is Wav2LipErrorEvent) {
      _giveUp(e.why);
    }
  }

  void _giveUp(String why) {
    if (_failed) return;
    _failed = true;
    backend.stop();
    _drop();
    _current = null;
    notifyListeners();
    fail(why);
  }

  @override
  void tick() {
    if (_failed) return;
    final played = player.playedMs;
    final due = played + leadMs;
    while (_queue.length > 1 && _queue.elementAt(1).at <= due) {
      _queue.removeFirst().image.dispose();
    }
    motion = IdleMotion(reduced: reducedMotion).at(_now());
    final heard = played < player.queuedMs;
    if (_queue.isNotEmpty && _queue.first.at <= due && heard) {
      if (!identical(_current, _queue.first.image)) _framesShown++;
      _current = _queue.first.image;
    } else if (!heard) {
      _current = null;
    }
    // Falling behind: the voice is being heard and the latest frame made is further behind it than the limit, for long enough.
    final lag = played - _latestAt;
    if ((heard || _open) && played > (_pendingBase ?? 0) && lag > wav2lipLagLimitMs) {
      _lagFrom = _lagFrom < 0 ? _now() : _lagFrom;
      if (_now() - _lagFrom > wav2lipLagHoldMs) _giveUp('too_slow');
    } else {
      _lagFrom = -1;
    }
    notifyListeners();
  }

  @override
  FaceScene? get scene {
    final layers = <FaceLayer>[];
    final cur = _current;
    if (cur != null) layers.add(FaceLayer(cur, pack.inv, const Size(96, 96), clip: pack.mouth));
    return FaceScene(poster: pack.poster, posterSize: pack.posterSize, motion: motion, layers: layers);
  }

  @override
  void dispose() {
    _sub?.cancel();
    backend.stop();
    _drop();
    super.dispose();
  }
}
