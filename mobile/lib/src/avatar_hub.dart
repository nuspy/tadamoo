import 'dart:io';

import 'package:flutter/foundation.dart';

import 'avatar_assets.dart';
import 'avatar_engines.dart';
import 'settings.dart';
import 'wav2lip_engine.dart';

/// What the app declares in its first message to the PC.
class RenderPlan {
  const RenderPlan(this.modes, {this.engine = '', this.resolution = 0});

  final List<String> modes;
  final String engine;
  final int resolution;

  Map<String, dynamic> get fields => {
        'render_modes': modes,
        if (engine.isNotEmpty) 'engine': engine,
        if (resolution > 0) 'resolution': resolution,
      };

  bool sameAs(RenderPlan o) => listEquals(modes, o.modes) && engine == o.engine && resolution == o.resolution;
}

/// How an engine stands on this phone, for the chooser in Settings. `ready` is the only state in which the mode is declared.
enum EngineStatus { ready, needsAssets, needsModel, downloading, notTested, testing, failed, noRuntime, unavailable }

/// What this phone can draw: the avatar's files, the Wav2Lip model and its self-test. It decides what the app declares (`plan`): a mode is
/// declared only when its renderer exists in this build, its files are on the phone and verified, and, for Wav2Lip, the self-test passed
/// here with this model.
class EngineHub extends ChangeNotifier {
  EngineHub({required this.settings, this.store, this.backend});

  final AppSettings settings;
  final AvatarAssetStore? store;
  final Wav2LipBackend? backend;

  bool runtime = false;
  bool testing = false;
  Wav2LipTestResult? lastTest;
  final Set<String> _suppressed = {};
  bool _disposed = false;

  String get avatarName => settings.avatarName;

  /// The assets of the avatar named last are whole and verified.
  bool get assetsReady {
    final s = store;
    return s != null && avatarName.isNotEmpty && s.stageOf(avatarName) == AssetStage.ready;
  }

  bool get visemeFiles => assetsReady && (store!.manifestOf(avatarName)?.hasVisemes ?? false);
  bool get wav2lipFiles => assetsReady && (store!.manifestOf(avatarName)?.hasWav2lip ?? false);
  bool get modelReady => store?.modelStage == ModelStage.ready;
  bool get testPassed => settings.wav2lipPassed == (store?.modelSha ?? wav2lipModelSha);

  bool get visemesReady => visemeFiles && !_suppressed.contains('visemes');
  bool get wav2lipReady => runtime && modelReady && wav2lipFiles && testPassed && !_suppressed.contains('wav2lip');

  /// The size on the phone of the files of the avatar named last, 0 when they are not all there.
  int assetBytes() {
    final st = store;
    if (st == null || !assetsReady) return 0;
    var n = 0;
    for (final rel in st.manifestOf(avatarName)?.files.keys ?? const <String>[]) {
      try {
        n += File(st.pathOf(avatarName, rel)).lengthSync();
      } on FileSystemException {
        return 0;
      }
    }
    return n;
  }

  /// Reads the disk and asks the runtime; call before connecting and after a download.
  Future<void> refresh() async {
    final s = store;
    if (s != null) {
      if (avatarName.isNotEmpty) await s.refresh(avatarName);
      await s.refreshModel();
    }
    runtime = backend == null ? false : await backend!.available();
    _tell();
  }

  /// A mode that failed on this phone is not declared again until the app is started again, or the test passes again.
  void suppress(String mode) {
    _suppressed.add(mode);
    _tell();
  }

  void unsuppress(String mode) {
    if (_suppressed.remove(mode)) _tell();
  }

  /// The first message: the modes the preference asks for and the phone can draw, best first, `video` last (the PC's emergency); the
  /// engine and height are the person's choice for the first of them.
  RenderPlan plan() {
    final pref = settings.avatarMode;
    final modes = <String>[];
    if (settings.avatarEnabled) {
      if (pref == 'wav2lip' && wav2lipReady) modes.add('wav2lip');
      if ((pref == 'wav2lip' || pref == 'visemes') && visemesReady) modes.add('visemes');
    }
    modes.add('video');
    final first = avatarEngines.where((e) => e.mode == modes.first && e.pcId.isNotEmpty);
    if (first.isEmpty) return RenderPlan(modes);
    final e = first.first;
    final h = settings.avatarResolution;
    return RenderPlan(modes, engine: e.pcId, resolution: e.heights.contains(h) ? h : 0);
  }

  /// The avatar's files, from the PC. True when they are whole afterwards.
  Future<bool> fetchAssets(String name) async {
    final s = store;
    if (s == null || name.isEmpty) return false;
    final ok = await s.sync(name);
    if (name != settings.avatarName) await settings.setAvatarName(name);
    _tell();
    return ok;
  }

  Future<bool> downloadModel() async {
    final s = store;
    if (s == null) return false;
    final ok = await s.downloadModel();
    if (ok) {
      // a new file has not passed the test
      await settings.setWav2lipTest(sha: '', fps: 0, ms: 0);
    }
    _tell();
    return ok;
  }

  /// The self-test and the benchmark. It needs the model; it uses the avatar's face when its files are here and its own face otherwise.
  /// Passing it (and only that) makes Wav2Lip declarable on this phone.
  Future<Wav2LipTestResult> runTest() async {
    final b = backend;
    final s = store;
    if (b == null || s == null || !modelReady) {
      lastTest = Wav2LipTestResult(ok: false, reason: b == null || !runtime ? 'no_runtime' : 'no_model');
      _tell();
      return lastTest!;
    }
    testing = true;
    _tell();
    final face = wav2lipFiles ? s.pathOf(avatarName, 'wav2lip/face.png') : null;
    Wav2LipTestResult r;
    try {
      r = await b.selfTest(model: s.modelFile.path, face: face, fps: 25);
    } catch (e) {
      r = Wav2LipTestResult(ok: false, reason: '$e');
    }
    testing = false;
    lastTest = r;
    await settings.setWav2lipTest(sha: r.ok ? s.modelSha : '', fps: r.fps, ms: r.msPerFrame);
    if (r.ok) unsuppress('wav2lip');
    _tell();
    return r;
  }

  /// The state of an engine of `avatarEngines` on this phone.
  EngineStatus status(AvatarEngine e) {
    if (!e.selectable) return EngineStatus.unavailable;
    switch (e.id) {
      case 'visemes':
        if (store == null) return EngineStatus.noRuntime;
        if (avatarName.isNotEmpty && store!.stageOf(avatarName) == AssetStage.downloading) return EngineStatus.downloading;
        return visemeFiles ? EngineStatus.ready : EngineStatus.needsAssets;
      case 'wav2lip':
        if (store == null || !runtime) return EngineStatus.noRuntime;
        if (store!.modelStage == ModelStage.downloading) return EngineStatus.downloading;
        if (!modelReady) return EngineStatus.needsModel;
        if (testing) return EngineStatus.testing;
        if (!wav2lipFiles) return EngineStatus.needsAssets;
        if (testPassed) return EngineStatus.ready;
        return lastTest != null && !lastTest!.ok ? EngineStatus.failed : EngineStatus.notTested;
      default:
        return EngineStatus.ready;
    }
  }

  void _tell() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
