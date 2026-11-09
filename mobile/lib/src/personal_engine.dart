import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/services.dart';

import 'avatar_assets.dart';
import 'avatar_renderer.dart';
import 'personal_pack.dart';
import 'wav2lip_engine.dart';

// Ultralight (with wenet) and FeatherTalk on the phone: models trained on the PC for one person, packaged there (voice/avatar_engines/phone_export.py),
// run with ONNX Runtime by Kotlin (PersonalEngine.kt) and drawn over the template frame like the Wav2Lip face. The model's licence terms are the
// upstream projects' (Ultralight-Digital-Human: Apache 2.0 by its README badge, no LICENSE file; FeatherTalk: Apache 2.0); this is the lab phase.

/// The self-test of a package on this phone: every file as listed, the models' outputs equal to the ones the PC measured, and the synthetic audio through the
/// whole path at a speed. The speed must reach the frame rate the model was trained at, else the frames would fall behind the voice.
class PersonalTestResult {
  const PersonalTestResult({required this.engine, required this.ok, this.reason = '', this.steps = const [], this.msPerFrame = 0, this.fps = 0, this.threads = 0, this.frames = 0, this.requiredFps = 0});

  final String engine;
  final bool ok;
  final String reason;
  final List<Wav2LipStep> steps;
  final double msPerFrame;
  final double fps;
  final int threads;
  final int frames;
  final double requiredFps;

  factory PersonalTestResult.fromMap(Map<Object?, Object?> m, {required String engine}) {
    final steps = [
      for (final s in (m['steps'] as List? ?? const []).whereType<Map>()) Wav2LipStep('${s['name']}', s['ok'] == true, '${s['detail'] ?? ''}'),
    ];
    final ms = (m['msPerFrame'] as num?)?.toDouble() ?? 0;
    final fps = (m['fps'] as num?)?.toDouble() ?? (ms > 0 ? 1000 / ms : 0);
    final need = (m['requiredFps'] as num?)?.toDouble() ?? 0;
    final functional = m['ok'] == true;
    final fast = need > 0 && fps >= need;
    return PersonalTestResult(
      engine: engine,
      ok: functional && fast,
      reason: functional && !fast ? 'too_slow' : '${m['reason'] ?? ''}',
      steps: steps,
      msPerFrame: ms,
      fps: fps,
      threads: (m['threads'] as num?)?.toInt() ?? 0,
      frames: (m['frames'] as num?)?.toInt() ?? 0,
      requiredFps: need,
    );
  }

  /// The report as plain English text to paste into a message.
  String toText() {
    final lines = <String>[
      'TaDaMoo avatar test ($engine on this phone)',
      'Result: ${ok ? 'passed' : 'failed${reason.isEmpty ? '' : ' ($reason)'}'}',
      for (final s in steps) '${s.ok ? 'ok  ' : 'FAIL'} ${s.name}${s.detail.isEmpty ? '' : ': ${s.detail}'}',
      if (msPerFrame > 0) 'Speed: ${msPerFrame.toStringAsFixed(1)} ms per frame, ${fps.toStringAsFixed(1)} frames/s over $frames frames, $threads threads',
      if (requiredFps > 0) 'Needed: at least ${requiredFps.toStringAsFixed(0)} frames/s',
      'The test checks that the files are the listed ones, that the models give the numbers the PC measured and how fast the whole path runs. It cannot tell whether the lips look right: look at the avatar.',
    ];
    return lines.join('\n');
  }
}

/// The personalised engines' runtime on the phone. The real one is Kotlin (PersonalChannel.kt); the tests use a stand-in.
abstract class PersonalBackend implements FrameBackend {
  /// Opens the package in `dir` (inside the app's folder); `sha` is the SHA-256 of its `phone.json` from the avatar's manifest. Kotlin checks every file again.
  Future<Wav2LipLoad> load({required String dir, required String sha});

  /// Runs the checks and the benchmark on the package in `dir`.
  Future<PersonalTestResult> selfTest({required String engine, required String dir, required String sha});
}

/// [PersonalBackend] over the Kotlin channels `app.basalt/personal` (calls) and `app.basalt/personal/events` (frames).
class ChannelPersonalBackend implements PersonalBackend {
  ChannelPersonalBackend({MethodChannel? channel, EventChannel? events})
      : _channel = channel ?? const MethodChannel('app.basalt/personal'),
        _events = events ?? const EventChannel('app.basalt/personal/events');

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
  Future<Wav2LipLoad> load({required String dir, required String sha}) async {
    try {
      final m = await _call<Map<Object?, Object?>>('load', {'dir': dir, 'sha': sha});
      if (m == null) return const Wav2LipLoad(false, 'no_runtime');
      return Wav2LipLoad(m['ok'] == true, '${m['reason'] ?? ''}', '${m['detail'] ?? ''}');
    } on PlatformException catch (e) {
      return Wav2LipLoad(false, 'onnx_load_failed', '${e.message}');
    }
  }

  @override
  Future<PersonalTestResult> selfTest({required String engine, required String dir, required String sha}) async {
    try {
      final m = await _call<Map<Object?, Object?>>('selfTest', {'dir': dir, 'sha': sha});
      if (m == null) return PersonalTestResult(engine: engine, ok: false, reason: 'no_runtime');
      return PersonalTestResult.fromMap(m, engine: engine);
    } on PlatformException catch (e) {
      return PersonalTestResult(engine: engine, ok: false, reason: e.message ?? e.code);
    }
  }

  @override
  Future<int> start(int sampleRate, int fps) async => await _call<int>('start', {'rate': sampleRate}) ?? 0;

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
  Stream<Wav2LipEvent> get events => _events.receiveBroadcastStream().map<Wav2LipEvent?>(wav2lipEventOf).where((e) => e != null).cast<Wav2LipEvent>();
}

/// The template frame of a verified package with where the drawn face goes on it, as the pack the Wav2Lip renderer lays its frames over; null when the
/// package is not verified on this phone or the picture does not decode.
Future<Wav2LipPack?> loadPersonalPack(AvatarAssetStore store, String name, String id, {Future<ui.Image> Function(Uint8List)? decoder}) async {
  final info = store.packOf(name, id);
  if (info == null) return null;
  try {
    final poster = await (decoder ?? VisemePack.decode)(await File('${store.engineDir(name, id)}/${info.frameFile}').readAsBytes());
    return Wav2LipPack(poster: poster, posterSize: info.frameSize, inv: info.inv, mouth: info.mask, facePath: '', size: info.size);
  } catch (_) {
    return null;
  }
}

/// How long one frame of this engine may take on average: the time it stands for.
double personalFrameBudgetMs(PersonalPackInfo info) => 1000.0 / info.fps;
