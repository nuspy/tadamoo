import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:archive/archive.dart';
import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_renderer.dart';
import 'package:basalt_mobile/src/wav2lip_engine.dart';
import 'package:crypto/crypto.dart';
import 'package:flutter/widgets.dart';

String sha(List<int> bytes) => sha256.convert(bytes).toString();

/// The files of an avatar as the PC serves them: a manifest with the SHA-256 of each file, and the zip.
class FakeAvatarFiles {
  FakeAvatarFiles(this.name, {bool wav2lip = true, bool visemes = true}) {
    files['poster.jpg'] = Uint8List.fromList(utf8.encode('poster'));
    if (visemes) {
      files['visemes/frame.json'] = Uint8List.fromList(utf8.encode(jsonEncode({'size': [10, 8], 'inv': [1, 0, 0, 0, 1, 0], 'poster_w': 40, 'poster_h': 30})));
      for (final v in visemeNames) {
        files['visemes/$v.png'] = Uint8List.fromList(utf8.encode('png-$v'));
      }
    }
    if (wav2lip) {
      files['wav2lip/face.png'] = Uint8List.fromList(utf8.encode('face'));
      files['wav2lip/box.json'] = Uint8List.fromList(utf8.encode(jsonEncode({'inv': [0.5, 0, 10, 0, 0.5, 4, ], 'mouth': {'x': 20, 'y': 60, 'w': 40, 'h': 20}, 'poster_w': 40, 'poster_h': 30})));
    }
  }

  final String name;
  final Map<String, Uint8List> files = {};

  Uint8List get manifest => Uint8List.fromList(utf8.encode(jsonEncode({
        'version': 1,
        'avatar': name,
        'created': 'now',
        'render_modes': [if (files.containsKey('wav2lip/box.json')) 'wav2lip', if (files.containsKey('visemes/frame.json')) 'visemes'],
        'files': {for (final e in files.entries) e.key: sha(e.value)},
      })));

  Uint8List get zip {
    final a = Archive();
    for (final e in files.entries) {
      a.add(ArchiveFile.bytes(e.key, e.value));
    }
    return Uint8List.fromList(ZipEncoder().encodeBytes(a));
  }
}

/// The PC's file routes in memory.
class FakeAssetSource implements AssetSource {
  final Map<String, Uint8List> routes = {};
  final List<String> asked = [];
  bool offline = false;
  Uint8List? modelBytes;
  String modelEtag = '';
  bool modelMissing = false;

  void serve(FakeAvatarFiles a) {
    routes['/api/voice/avatar/${a.name}/manifest.json'] = a.manifest;
    routes['/api/voice/avatar/${a.name}/assets.zip'] = a.zip;
  }

  @override
  Future<Uint8List> bytes(String path) async {
    asked.add(path);
    if (offline) throw AssetError('network');
    final b = routes[path];
    if (b == null) throw AssetError('missing', path);
    return b;
  }

  @override
  Future<ModelFetch> model(String path, File part, {required void Function(int got, int total) progress}) async {
    asked.add(path);
    if (offline) throw AssetError('network');
    if (modelMissing || modelBytes == null) throw AssetError('not_installed');
    part.parent.createSync(recursive: true);
    part.writeAsBytesSync(modelBytes!);
    progress(modelBytes!.length, modelBytes!.length);
    return ModelFetch(modelEtag.isEmpty ? sha(modelBytes!) : modelEtag, modelBytes!.length);
  }
}

/// The Wav2Lip runtime in memory: what it was asked, and what it answers.
class FakeWav2LipBackend implements Wav2LipBackend {
  final StreamController<Wav2LipEvent> controller = StreamController<Wav2LipEvent>.broadcast();
  final List<String> calls = [];
  final List<Uint8List> fed = [];
  bool runtime = true;
  Wav2LipLoad loadResult = const Wav2LipLoad(true);
  Wav2LipTestResult testResult = const Wav2LipTestResult(ok: true, fps: 30, msPerFrame: 33, threads: 4, frames: 40, steps: [Wav2LipStep('model', true, 'loaded')]);
  int gen = 0;

  @override
  Future<bool> available() async => runtime;

  @override
  Future<Wav2LipLoad> load({required String model, required String face}) async {
    calls.add('load');
    return loadResult;
  }

  @override
  Future<Wav2LipTestResult> selfTest({String? model, String? face, required int fps}) async {
    calls.add('selfTest ${face != null}');
    return testResult;
  }

  @override
  Future<int> start(int sampleRate, int fps) async {
    calls.add('start $sampleRate $fps');
    return ++gen;
  }

  @override
  void feed(Uint8List pcm16) => fed.add(pcm16);

  @override
  Future<void> finish() async => calls.add('finish');

  @override
  Future<void> stop() async => calls.add('stop');

  @override
  Future<void> unload() async => calls.add('unload');

  @override
  Stream<Wav2LipEvent> get events => controller.stream;

  void frame(int g, int tMs) => controller.add(Wav2LipFrameEvent(Wav2LipFrame(g, tMs, 2, 2, Uint8List(16))));
}

/// A renderer that only records what it is told.
class FakeRenderer extends AvatarRenderer {
  FakeRenderer(this.kind);
  final String kind;
  final List<String> calls = [];
  final List<Map<String, dynamic>> timelines = [];
  bool disposed = false;

  @override
  String get mode => kind;

  @override
  void audioStart(int sampleRate) => calls.add('start $sampleRate');

  @override
  void visemes(Map<String, dynamic> message) => timelines.add(message);

  @override
  void audio(Uint8List pcm16) => calls.add('audio ${pcm16.length}');

  @override
  void audioEnd() => calls.add('end');

  @override
  void flush() => calls.add('flush');

  @override
  Widget view() => SizedBox(key: ValueKey('renderer-$kind'), width: 10, height: 10);

  @override
  FaceScene? get scene => null;

  @override
  void dispose() {
    disposed = true;
    super.dispose();
  }
}

/// A small real picture, for the painters and the packs (decoding needs the engine: create it inside `tester.runAsync`).
Future<ui.Image> tinyImage([int w = 4, int h = 4, ui.Color color = const ui.Color(0xFF336699)]) async {
  final recorder = ui.PictureRecorder();
  ui.Canvas(recorder).drawRect(ui.Rect.fromLTWH(0, 0, w.toDouble(), h.toDouble()), ui.Paint()..color = color);
  return recorder.endRecording().toImage(w, h);
}
