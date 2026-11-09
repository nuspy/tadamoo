import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:basalt_mobile/src/audio_route.dart';
import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/avatar_hub.dart';
import 'package:basalt_mobile/src/avatar_settings.dart';
import 'package:basalt_mobile/src/avatar_engines.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/personal_engine.dart';
import 'package:basalt_mobile/src/personal_pack.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/wav2lip_engine.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' hide AssetManifest;
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';
import 'secret_fakes.dart';

const prefix = 'engines/feathertalk/export';

/// A package as the PC makes it (voice/avatar_engines/phone_export.py), with files of a few bytes: phone.json and the files it lists.
class FakePackage {
  FakePackage({required Uint8List frame}) {
    files['feathertalk_144.onnx'] = Uint8List.fromList(utf8.encode('visual model'));
    files['feather_hubert.onnx'] = Uint8List.fromList(utf8.encode('audio model'));
    files['frames/0.jpg'] = frame;
    files['phone/template.bin'] = Uint8List(6 * 4 * 4);
    files['phone/selftest/audio.f32'] = Uint8List(32);
    phone = {
      'version': 1,
      'engine': 'feathertalk',
      'fps': 25,
      'size': 144,
      'frontend': {'kind': 'feather_hubert'},
      'model': {'file': 'feathertalk_144.onnx'},
      'template': {'input': 'phone/template.bin', 'frame': 'frames/0.jpg', 'frame_w': 320, 'frame_h': 300, 'inv': [0.5, 0, 60, 0, 0.5, 70], 'mask': {'x': 4, 'y': 4, 'w': 135, 'h': 130}},
      'selftest': {'tolerance': {'rtol': 0.02, 'atol': 0.002}, 'stages': [{'name': 'audio'}, {'name': 'visual'}]},
      'files': {for (final e in files.entries) e.key: {'sha256': sha(e.value), 'bytes': e.value.length}},
    };
  }

  final Map<String, Uint8List> files = {};
  late Map<String, Object?> phone;

  Uint8List get phoneBytes => Uint8List.fromList(utf8.encode(jsonEncode(phone)));

  /// The `engines` entry of the avatar's manifest.
  Map<String, Object?> get entry => {
        'files': {
          '$prefix/phone.json': sha(phoneBytes),
          for (final e in files.entries) '$prefix/${e.key}': sha(e.value),
        },
      };
}

/// The PC's routes in memory: the avatar's manifest with an `engines` entry, and the package files one by one.
class PackSource extends FakeAssetSource {
  PackSource(this.avatar, this.pack, {this.engines = true});
  final FakeAvatarFiles avatar;
  final FakePackage pack;
  final bool engines;
  final Map<String, Uint8List> served = {};

  void publish() {
    final base = jsonDecode(utf8.decode(avatar.manifest)) as Map<String, Object?>;
    if (engines) {
      base['engines'] = {'feathertalk': pack.entry};
      base['render_modes'] = [...(base['render_modes'] as List), 'feathertalk'];
    }
    routes['/api/voice/avatar/ada/manifest.json'] = Uint8List.fromList(utf8.encode(jsonEncode(base)));
    routes['/api/voice/avatar/ada/assets.zip'] = avatar.zip;
    routes['/api/voice/avatar/ada/engines/feathertalk/files/phone.json'] = pack.phoneBytes;
    served.addAll({for (final e in pack.files.entries) '/api/voice/avatar/ada/engines/feathertalk/files/${e.key}': e.value});
  }

  @override
  Future<ModelFetch> model(String path, File part, {required void Function(int got, int total) progress}) async {
    asked.add(path);
    if (offline) throw AssetError('network');
    final bytes = served[path];
    if (bytes == null) throw AssetError('not_installed');
    part.parent.createSync(recursive: true);
    part.writeAsBytesSync(bytes);
    progress(bytes.length, bytes.length);
    return ModelFetch(sha(bytes), bytes.length);
  }
}

/// The personalised engines' runtime in memory.
class FakePersonalBackend implements PersonalBackend {
  final StreamController<Wav2LipEvent> controller = StreamController<Wav2LipEvent>.broadcast();
  final List<String> calls = [];
  bool runtime = true;
  Wav2LipLoad loadResult = const Wav2LipLoad(true);
  Map<String, Object?> testAnswer = {'ok': true, 'msPerFrame': 20.0, 'requiredFps': 25.0, 'threads': 4, 'frames': 30, 'steps': [{'name': 'package', 'ok': true, 'detail': 'listed'}]};

  @override
  Future<bool> available() async => runtime;

  @override
  Future<Wav2LipLoad> load({required String dir, required String sha}) async {
    calls.add('load $sha');
    return loadResult;
  }

  @override
  Future<PersonalTestResult> selfTest({required String engine, required String dir, required String sha}) async {
    calls.add('selfTest $engine');
    return PersonalTestResult.fromMap(testAnswer, engine: engine);
  }

  @override
  Future<int> start(int sampleRate, int fps) async {
    calls.add('start $sampleRate $fps');
    return 1;
  }

  @override
  void feed(Uint8List pcm16) {}

  @override
  Future<void> finish() async => calls.add('finish');

  @override
  Future<void> stop() async => calls.add('stop');

  @override
  Future<void> unload() async => calls.add('unload');

  @override
  Stream<Wav2LipEvent> get events => controller.stream;
}

Future<Uint8List> pngBytes() async {
  final image = await tinyImage(4, 4);
  final data = await image.toByteData(format: ui.ImageByteFormat.png);
  return data!.buffer.asUint8List();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('phone.json is read strictly', () {
    late FakePackage pack;
    setUp(() => pack = FakePackage(frame: Uint8List.fromList([1, 2, 3])));

    PersonalPackInfo? read(Map<String, Object?> j, [String engine = 'feathertalk']) => PersonalPackInfo.parse(Uint8List.fromList(utf8.encode(jsonEncode(j))), engine);

    test('a good package gives the frame rate, the face, where it goes and every file', () {
      final info = read(pack.phone)!;
      expect(info.fps, 25);
      expect(info.size, 144);
      expect(info.kind, 'feather_hubert');
      expect(info.frameFile, 'frames/0.jpg');
      expect(info.frameSize, const Size(320, 300));
      expect(info.inv, [0.5, 0, 60, 0, 0.5, 70]);
      expect(info.mask, const Rect.fromLTWH(4, 4, 135, 130));
      expect(info.files.keys, containsAll(['feathertalk_144.onnx', 'feather_hubert.onnx', 'frames/0.jpg', 'phone/template.bin']));
      expect(info.bytes, pack.files.values.fold<int>(0, (a, b) => a + b.length));
    });

    test('another engine, version, front end or a path that climbs makes it unusable', () {
      expect(read(pack.phone, 'ultralight'), isNull, reason: 'it is the package of another engine');
      expect(read({...pack.phone, 'version': 2}), isNull);
      expect(read({...pack.phone, 'frontend': {'kind': 'hubert_large'}}), isNull, reason: 'HuBERT Large cannot run on a phone');
      expect(read({...pack.phone, 'fps': 0}), isNull);
      expect(read({...pack.phone, 'files': {...(pack.phone['files'] as Map), '../x': {'sha256': 'a' * 64, 'bytes': 1}}}), isNull);
      expect(read({...pack.phone, 'files': {...(pack.phone['files'] as Map), 'a.bin': {'sha256': 'xyz', 'bytes': 1}}}), isNull);
      expect(PersonalPackInfo.parse(Uint8List.fromList(utf8.encode('{')), 'feathertalk'), isNull);
      final noFrame = {...pack.phone, 'files': {for (final e in (pack.phone['files'] as Map).entries) if (e.key != 'frames/0.jpg') e.key: e.value}};
      expect(read(noFrame), isNull, reason: 'the template frame must be one of the listed files');
    });
  });

  group('the self-test result', () {
    test('it passes only when the checks passed and the speed reaches the frame rate of the model', () {
      Map<String, Object?> m(double ms, {bool ok = true}) => {'ok': ok, 'msPerFrame': ms, 'requiredFps': 25.0, 'threads': 4, 'frames': 30, 'steps': [{'name': 'audio', 'ok': ok, 'detail': 'the numbers of the PC'}]};
      expect(PersonalTestResult.fromMap(m(30), engine: 'feathertalk').ok, isTrue, reason: '33 frames/s');
      final slow = PersonalTestResult.fromMap(m(60), engine: 'feathertalk');
      expect(slow.ok, isFalse);
      expect(slow.reason, 'too_slow');
      final wrong = PersonalTestResult.fromMap({...m(30, ok: false), 'reason': 'numeric_mismatch'}, engine: 'ultralight');
      expect(wrong.ok, isFalse);
      expect(wrong.reason, 'numeric_mismatch');
      expect(PersonalTestResult.fromMap({'ok': true, 'msPerFrame': 5.0}, engine: 'feathertalk').ok, isFalse, reason: 'with no frame rate to reach there is nothing to pass');
    });

    test('the text says what was checked and what it cannot judge', () {
      final t = PersonalTestResult.fromMap({'ok': true, 'msPerFrame': 20.0, 'requiredFps': 25.0, 'threads': 2, 'frames': 31, 'steps': [{'name': 'package', 'ok': true, 'detail': 'listed'}]}, engine: 'feathertalk').toText();
      expect(t, contains('feathertalk on this phone'));
      expect(t, contains('Result: passed'));
      expect(t, contains('ok   package: listed'));
      expect(t, contains('50.0 frames/s over 31 frames, 2 threads'));
      expect(t, contains('Needed: at least 25 frames/s'));
      expect(t, contains('cannot tell whether the lips look right'));
    });
  });

  group('the time budget of a frame', () {
    test('it trips only when a whole window of frames costs more on average than the time they stand for', () {
      final b = FrameBudget(40, window: 4);
      expect([for (final ms in [100, 100, 100]) b.add(ms)], [false, false, false], reason: 'not a full window yet');
      expect(b.add(100), isTrue);
      expect([for (final ms in [10, 10, 10, 10]) b.add(ms)], [true, true, false, false], reason: 'the slow ones leave the window one by one');
    });

    test('one slow frame, such as the one that pays for an audio block, does not trip it', () {
      final b = FrameBudget(40, window: 25);
      final results = [for (var i = 0; i < 100; i++) b.add(i % 25 == 0 ? 400 : 20)];
      expect(results.any((r) => r), isFalse, reason: '(400 + 24 x 20) / 25 = 35 ms, under 40');
    });
  });

  group('a renderer that gives the mode up when the frames cost too much', () {
    late FakePersonalBackend backend;
    late FakePlayer player;
    late Wav2LipPack pack;

    setUp(() async {
      backend = FakePersonalBackend();
      player = FakePlayer();
      pack = Wav2LipPack(poster: await tinyImage(), posterSize: const Size(320, 300), inv: const [0.5, 0, 60, 0, 0.5, 70], mouth: const Rect.fromLTWH(4, 4, 135, 130), facePath: '', size: 144);
    });

    Wav2LipRenderer make(String? why) {
      final r = Wav2LipRenderer(backend: backend, pack: pack, player: player, modeId: 'feathertalk', fps: 25, frameBudgetMs: 40, toImage: (f) async => tinyImage());
      r.onFailed = (w) => why = w;
      return r;
    }

    Future<void> frames(int n, int ms, {int from = 0}) async {
      for (var i = 0; i < n; i++) {
        backend.controller.add(Wav2LipFrameEvent(Wav2LipFrame(1, (from + i) * 40, 2, 2, Uint8List(16), ms: ms)));
        await pumpEventQueue();
      }
    }

    test('it is the engine named, with the face the size the model draws', () async {
      final r = make(null);
      addTearDown(r.dispose);
      expect(r.mode, 'feathertalk');
      expect(r.pack.size, 144);
    });

    test('frames that cost more than 40 ms each, for a window, give the mode up with too_slow and stop the engine', () async {
      String? reason;
      final r = Wav2LipRenderer(backend: backend, pack: pack, player: player, modeId: 'feathertalk', fps: 25, frameBudgetMs: 40, toImage: (f) async => tinyImage());
      r.onFailed = (w) => reason = w;
      addTearDown(r.dispose);
      await frames(24, 80);
      expect(reason, isNull, reason: 'a window is 25 frames');
      await frames(1, 80, from: 24);
      expect(reason, 'too_slow');
      expect(backend.calls, contains('stop'));
    });

    test('frames within the budget, and frames that do not say what they cost, never give it up', () async {
      String? reason;
      final r = Wav2LipRenderer(backend: backend, pack: pack, player: player, modeId: 'feathertalk', fps: 25, frameBudgetMs: 40, toImage: (f) async => tinyImage());
      r.onFailed = (w) => reason = w;
      addTearDown(r.dispose);
      await frames(60, 30);
      await frames(60, -1);
      expect(reason, isNull);
    });
  });

  group('the package on the phone', () {
    late Directory dir;
    late FakeAvatarFiles avatar;
    late FakePackage pack;
    late PackSource source;
    late AvatarAssetStore store;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('basalt_personal');
      avatar = FakeAvatarFiles('ada');
      pack = FakePackage(frame: await pngBytes());
      source = PackSource(avatar, pack)..publish();
      store = AvatarAssetStore(root: dir, source: source);
      expect(await store.sync('ada'), isTrue);
    });

    tearDown(() {
      store.dispose();
      dir.deleteSync(recursive: true);
    });

    test('the manifest names the engine and its files, and keeps them after a restart', () async {
      expect(store.offersEngine('ada', 'feathertalk'), isTrue);
      expect(store.offersEngine('ada', 'ultralight'), isFalse);
      expect(store.phoneShaOf('ada', 'feathertalk'), sha(pack.phoneBytes));
      final again = AvatarAssetStore(root: dir, source: source);
      addTearDown(again.dispose);
      await again.refresh('ada');
      expect(again.offersEngine('ada', 'feathertalk'), isTrue);
      expect(again.manifestOf('ada')!.files.keys, isNot(contains('$prefix/phone.json')), reason: 'the package is not in the zip');
    });

    test('a manifest entry with a path outside the engine\'s folder or without phone.json is left out', () {
      Uint8List manifest(Map<String, String> files) => Uint8List.fromList(utf8.encode(jsonEncode({'version': 1, 'avatar': 'a', 'render_modes': [], 'files': {}, 'engines': {'feathertalk': {'files': files}}})));
      final good = {'$prefix/phone.json': 'a' * 64};
      expect(AssetManifest.parse(manifest(good))!.engines.keys, ['feathertalk']);
      expect(AssetManifest.parse(manifest({...good, 'poster.jpg': 'a' * 64}))!.engines, isEmpty);
      expect(AssetManifest.parse(manifest({...good, '$prefix/../x': 'a' * 64}))!.engines, isEmpty);
      expect(AssetManifest.parse(manifest({'$prefix/model.onnx': 'a' * 64}))!.engines, isEmpty, reason: 'no phone.json');
    });

    test('the files are fetched one by one, checked against phone.json and the manifest, and the package is then ready', () async {
      expect(store.engineStageOf('ada', 'feathertalk'), EngineStage.missing);
      expect(await store.syncEngine('ada', 'feathertalk'), isTrue);
      expect(store.engineStageOf('ada', 'feathertalk'), EngineStage.ready);
      final info = store.packOf('ada', 'feathertalk')!;
      expect(info.fps, 25);
      for (final e in pack.files.entries) {
        expect(File('${store.engineDir('ada', 'feathertalk')}/${e.key}').readAsBytesSync(), e.value);
      }
      expect(source.asked.where((p) => p.contains('/engines/')).length, 1 + pack.files.length);
      source.asked.clear();
      expect(await store.syncEngine('ada', 'feathertalk'), isTrue);
      expect(source.asked, isEmpty, reason: 'what is there and verified is not fetched again');
    });

    test('a file that is not what phone.json says is refused and nothing of it is kept', () async {
      source.served['/api/voice/avatar/ada/engines/feathertalk/files/feathertalk_144.onnx'] = Uint8List.fromList(utf8.encode('visual modeX'));
      expect(await store.syncEngine('ada', 'feathertalk'), isFalse);
      expect(store.engineReason, 'sha');
      expect(store.engineStageOf('ada', 'feathertalk'), EngineStage.failed);
      expect(store.packOf('ada', 'feathertalk'), isNull);
      expect(File('${store.engineDir('ada', 'feathertalk')}/feathertalk_144.onnx').existsSync(), isFalse);
      expect(File('${store.engineDir('ada', 'feathertalk')}/feathertalk_144.onnx.part').existsSync(), isFalse);
    });

    test('a phone.json that is not the one the manifest states is refused', () async {
      pack.phone['fps'] = 20;
      source.routes['/api/voice/avatar/ada/engines/feathertalk/files/phone.json'] = pack.phoneBytes;
      final other = AvatarAssetStore(root: Directory.systemTemp.createTempSync('basalt_personal2'), source: source);
      addTearDown(() {
        other.root.deleteSync(recursive: true);
        other.dispose();
      });
      await other.sync('ada');
      source.routes['/api/voice/avatar/ada/engines/feathertalk/files/phone.json'] = pack.phoneBytes;
      expect(await other.syncEngine('ada', 'feathertalk'), isFalse);
      expect(other.engineReason, 'sha');
    });

    test('a package whose files disagree with the manifest is refused', () async {
      pack.files['phone/template.bin'] = Uint8List(6 * 4 * 4 + 1);
      pack.phone['files'] = {for (final e in pack.files.entries) e.key: {'sha256': sha(e.value), 'bytes': e.value.length}};
      // phone.json says one thing, the manifest (made before) another
      final stale = PackSource(avatar, FakePackage(frame: pack.files['frames/0.jpg']!))..publish();
      stale.routes['/api/voice/avatar/ada/engines/feathertalk/files/phone.json'] = Uint8List.fromList(utf8.encode(jsonEncode({...pack.phone})));
      final other = AvatarAssetStore(root: Directory.systemTemp.createTempSync('basalt_personal3'), source: stale);
      addTearDown(() {
        other.root.deleteSync(recursive: true);
        other.dispose();
      });
      await other.sync('ada');
      expect(await other.syncEngine('ada', 'feathertalk'), isFalse);
      expect(other.engineReason, anyOf('sha', 'manifest'));
    });

    test('a file changed on the phone after it was verified makes the package not ready again', () async {
      await store.syncEngine('ada', 'feathertalk');
      File('${store.engineDir('ada', 'feathertalk')}/feather_hubert.onnx').writeAsBytesSync([9, 9, 9]);
      expect(await store.refreshEngine('ada', 'feathertalk'), isFalse);
      expect(store.engineStageOf('ada', 'feathertalk'), EngineStage.missing);
      expect(store.packOf('ada', 'feathertalk'), isNull);
      expect(await store.syncEngine('ada', 'feathertalk'), isTrue, reason: 'the file is fetched again');
    });

    test('offline, the package is not made ready and says so', () async {
      source.offline = true;
      expect(await store.syncEngine('ada', 'feathertalk'), isFalse);
      expect(store.engineReason, 'network');
    });

    test('the outcome of a self-test is kept for the package that passed and no other', () async {
      await store.syncEngine('ada', 'feathertalk');
      expect(store.personalPassed('ada', 'feathertalk'), isFalse);
      await store.savePersonalTest('ada', 'feathertalk', PersonalTestRecord(sha(pack.phoneBytes), 40, 25));
      expect(store.personalPassed('ada', 'feathertalk'), isTrue);
      final again = AvatarAssetStore(root: dir, source: source);
      addTearDown(again.dispose);
      await again.refresh('ada');
      await again.refreshEngine('ada', 'feathertalk');
      expect(again.personalPassed('ada', 'feathertalk'), isTrue);
      expect(again.personalTestOf('ada', 'feathertalk')!.fps, 40);
      await store.savePersonalTest('ada', 'feathertalk', PersonalTestRecord('a' * 64, 40, 25));
      expect(store.personalPassed('ada', 'feathertalk'), isFalse, reason: 'another phone.json did not pass');
    });

    test('the template frame and where the face goes become the pack the renderer lays its frames over', () async {
      await store.syncEngine('ada', 'feathertalk');
      final p = (await loadPersonalPack(store, 'ada', 'feathertalk'))!;
      expect(p.size, 144);
      expect(p.posterSize, const Size(320, 300));
      expect(p.inv, [0.5, 0, 60, 0, 0.5, 70]);
      expect(p.mouth, const Rect.fromLTWH(4, 4, 135, 130));
    });
  });

  group('what the app declares', () {
    late Directory dir;
    late AppSettings settings;
    late FakeAvatarFiles avatar;
    late FakePackage pack;
    late PackSource source;
    late AvatarAssetStore store;
    late FakePersonalBackend personal;
    late EngineHub hub;

    Future<void> make({String mode = 'feathertalk', bool engines = true}) async {
      Texts.use('en');
      SharedPreferences.setMockInitialValues({'flutter.avatarMode': mode, 'flutter.avatarName': 'ada'});
      settings = await AppSettings.load(secrets: FakeSecretStore());
      dir = Directory.systemTemp.createTempSync('basalt_declare');
      avatar = FakeAvatarFiles('ada');
      pack = FakePackage(frame: await pngBytes());
      source = PackSource(avatar, pack, engines: engines)..publish();
      store = AvatarAssetStore(root: dir, source: source);
      personal = FakePersonalBackend();
      hub = EngineHub(settings: settings, store: store, backend: FakeWav2LipBackend(), personal: personal);
    }

    tearDown(() {
      hub.dispose();
      store.dispose();
      dir.deleteSync(recursive: true);
    });

    AvatarEngine engine(String id) => avatarEngines.firstWhere((e) => e.id == id);

    test('the engine is declared only when its package is verified here and its self-test passed with exactly that package, after the visemes it falls back to are ready', () async {
      await make();
      await hub.fetchAssets('ada');
      await hub.refresh();
      expect(hub.status(engine('feathertalk')), EngineStatus.needsPackage);
      expect(hub.plan().modes, ['visemes', 'video'], reason: 'nothing to declare of the engine yet');
      await hub.fetchPersonal('feathertalk');
      expect(hub.status(engine('feathertalk')), EngineStatus.notTested);
      expect(hub.plan().modes, ['visemes', 'video'], reason: 'the files are not enough');
      final r = await hub.runPersonalTest('feathertalk');
      expect(r.ok, isTrue);
      expect(personal.calls, contains('selfTest feathertalk'));
      expect(hub.status(engine('feathertalk')), EngineStatus.ready);
      expect(hub.plan().modes, ['feathertalk', 'visemes', 'video'], reason: 'the chain: the engine, then the visemes, then the PC\'s video');
      expect(hub.plan().engine, isEmpty, reason: 'the PC is not told an engine id it has no mode for');
    });

    test('a test that fails, or a slow phone, leaves it undeclared and says why', () async {
      await make();
      await hub.fetchAssets('ada');
      await hub.refresh();
      await hub.fetchPersonal('feathertalk');
      personal.testAnswer = {...personal.testAnswer, 'ok': false, 'reason': 'numeric_mismatch'};
      expect((await hub.runPersonalTest('feathertalk')).reason, 'numeric_mismatch');
      expect(hub.status(engine('feathertalk')), EngineStatus.failed);
      expect(hub.plan().modes, ['visemes', 'video']);
      personal.testAnswer = {...personal.testAnswer, 'ok': true, 'msPerFrame': 80.0};
      expect((await hub.runPersonalTest('feathertalk')).reason, 'too_slow');
      expect(hub.personalReady('feathertalk'), isFalse);
    });

    test('a mode that failed here is not declared again until the test passes again; a new phone.json has not passed', () async {
      await make();
      await hub.fetchAssets('ada');
      await hub.refresh();
      await hub.fetchPersonal('feathertalk');
      await hub.runPersonalTest('feathertalk');
      expect(hub.personalReady('feathertalk'), isTrue);
      hub.suppress('feathertalk');
      expect(hub.plan().modes, ['visemes', 'video']);
      await hub.runPersonalTest('feathertalk');
      expect(hub.personalReady('feathertalk'), isTrue);
      await store.savePersonalTest('ada', 'feathertalk', PersonalTestRecord('b' * 64, 40, 25));
      expect(hub.personalReady('feathertalk'), isFalse);
    });

    test('a PC that has no package for this avatar, and a build with no runtime, say so and declare nothing', () async {
      await make(engines: false);
      await hub.fetchAssets('ada');
      await hub.refresh();
      expect(hub.status(engine('feathertalk')), EngineStatus.notExported);
      expect(hub.plan().modes, ['visemes', 'video']);
      personal.runtime = false;
      await hub.refresh();
      expect(hub.status(engine('feathertalk')), EngineStatus.noRuntime);
    });

    test('the other engines\' preference does not declare it, and ready from an earlier run is found again on disk', () async {
      await make(mode: 'visemes');
      await hub.fetchAssets('ada');
      await hub.refresh();
      await hub.fetchPersonal('feathertalk');
      await hub.runPersonalTest('feathertalk');
      expect(hub.plan().modes, ['visemes', 'video']);
      final again = EngineHub(settings: settings, store: AvatarAssetStore(root: dir, source: source), personal: personal);
      addTearDown(again.dispose);
      expect(again.personalReady('feathertalk'), isFalse, reason: 'nothing is claimed before the disk was read');
      await again.refresh();
      expect(again.personalReady('feathertalk'), isTrue, reason: 'verified again from the files, and its test is remembered for exactly this phone.json');
      expect(again.plan().modes, ['visemes', 'video'], reason: 'but the preference is the visemes');
    });
  });

  group('the chain: the engine, then the visemes, then the PC\'s video', () {
    late Directory dir;
    late AppSettings settings;
    late PackSource source;
    late AvatarAssetStore store;
    late FakePersonalBackend personal;
    late EngineHub hub;
    late FakeDevices devices;
    late FakeConnector connector;
    late AvatarController c;
    late FakePackage pack;

    Future<void> make({bool pass = true}) async {
      Texts.use('en');
      SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', 'flutter.avatarMode': 'feathertalk', 'flutter.avatarName': 'ada'});
      settings = await AppSettings.load(secrets: FakeSecretStore());
      dir = Directory.systemTemp.createTempSync('basalt_chain');
      pack = FakePackage(frame: await pngBytes());
      source = PackSource(FakeAvatarFiles('ada'), pack)..publish();
      store = AvatarAssetStore(root: dir, source: source);
      personal = FakePersonalBackend();
      hub = EngineHub(settings: settings, store: store, backend: FakeWav2LipBackend(), personal: personal);
      await hub.fetchAssets('ada');
      await hub.refresh();
      await hub.fetchPersonal('feathertalk');
      if (pass) await hub.runPersonalTest('feathertalk');
      devices = FakeDevices();
      connector = FakeConnector();
      c = AvatarController(api: FakeGatewayApi(settings), settings: settings, devices: devices, client: GatewayClient(connector: connector.call, wait: (d) async {}), hub: hub);
    }

    tearDown(() {
      c.dispose();
      hub.dispose();
      store.dispose();
      dir.deleteSync(recursive: true);
    });

    Future<void> until(bool Function() done) async {
      for (var i = 0; i < 100 && !done(); i++) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      await pumpEventQueue();
    }

    test('the first message offers the engine, the visemes and the video; the PC picks the engine; Kotlin is asked to load exactly the verified package', () async {
      await make();
      await c.activate();
      await pumpEventQueue();
      final ch = connector.channel;
      expect(ch.sent.first['render_modes'], ['feathertalk', 'visemes', 'video']);
      ch.say({...readyMessage, 'avatar': false, 'render_mode': 'feathertalk', 'render_modes': ['feathertalk', 'visemes', 'video'], 'avatar_name': 'ada'});
      await until(() => personal.calls.any((x) => x.startsWith('load')));
      expect(personal.calls.firstWhere((x) => x.startsWith('load')), 'load ${sha(pack.phoneBytes)}');
      await until(() => c.renderer != null);
      expect(c.renderer!.mode, 'feathertalk');
    });

    test('a package that Kotlin refuses is a failure of its mode with the reason, and the PC moves to the visemes', () async {
      await make();
      personal.loadResult = const Wav2LipLoad(false, 'hash_mismatch', 'feather_hubert.onnx');
      await c.activate();
      await pumpEventQueue();
      final ch = connector.channel;
      ch.say({...readyMessage, 'avatar': false, 'render_mode': 'feathertalk', 'render_modes': ['feathertalk', 'visemes', 'video'], 'avatar_name': 'ada'});
      await until(() => ch.last('render_failed') != null);
      expect(ch.last('render_failed'), {'type': 'render_failed', 'mode': 'feathertalk', 'why': 'hash_mismatch'});
      expect(hub.personalReady('feathertalk'), isFalse);
    });

    test('too slow while drawing: the renderer gives up, the PC is told and the mode is not declared again', () async {
      await make();
      await c.activate();
      await pumpEventQueue();
      final ch = connector.channel;
      ch.say({...readyMessage, 'avatar': false, 'render_mode': 'feathertalk', 'render_modes': ['feathertalk', 'visemes', 'video'], 'avatar_name': 'ada'});
      await until(() => c.renderer != null);
      for (var i = 0; i < 25; i++) {
        personal.controller.add(Wav2LipFrameEvent(Wav2LipFrame(1, i * 40, 2, 2, Uint8List(16), ms: 90)));
        await pumpEventQueue();
      }
      await until(() => ch.last('render_failed') != null);
      expect(ch.last('render_failed'), {'type': 'render_failed', 'mode': 'feathertalk', 'why': 'too_slow'});
      expect(hub.personalReady('feathertalk'), isFalse);
      expect(hub.plan().modes, ['visemes', 'video']);
    });

    test('without a passed test the first message does not name the engine', () async {
      await make(pass: false);
      await c.activate();
      await pumpEventQueue();
      expect(connector.channel.sent.first['render_modes'], ['visemes', 'video']);
    });
  });

  for (final size in const [Size(360, 640), Size(390, 844)]) {
    testWidgets('${size.width.toInt()}x${size.height.toInt()} at text 2x: the chooser gets the package, tests it and shows it ready, with buttons of 48 dp', (tester) async {
      Texts.use('en');
      tester.view.physicalSize = Size(size.width * 3, size.height * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      SharedPreferences.setMockInitialValues({'flutter.avatarName': 'ada'});
      final settings = await AppSettings.load(secrets: FakeSecretStore());
      final dir = Directory.systemTemp.createTempSync('basalt_choose');
      final pack = FakePackage(frame: await tester.runAsync(pngBytes) as Uint8List);
      final source = PackSource(FakeAvatarFiles('ada'), pack)..publish();
      final store = AvatarAssetStore(root: dir, source: source);
      final personal = FakePersonalBackend();
      final hub = EngineHub(settings: settings, store: store, backend: FakeWav2LipBackend(), personal: personal);
      final route = AudioRouteController(router: FakeRouter(), settings: settings);
      addTearDown(() async {
        route.dispose();
        hub.dispose();
        store.dispose();
        await Future<void>.delayed(const Duration(milliseconds: 30));
        try {
          dir.deleteSync(recursive: true);
        } on FileSystemException {
          // a temp folder Windows still holds open: the system cleans it
        }
      });
      await tester.runAsync(() async {
        await hub.fetchAssets('ada');
        await hub.refresh();
      });
      await tester.pumpWidget(MaterialApp(
        theme: ThemeData.dark(),
        builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: const TextScaler.linear(2.0)), child: child!),
        home: Scaffold(body: SingleChildScrollView(padding: const EdgeInsets.all(16), child: AvatarVoiceSettings(settings: settings, route: route, hub: hub, loadPersonas: () async => const []))),
      ));
      await tester.pump();
      await tester.pump();

      final fetch = find.byKey(const ValueKey('engine-feathertalk-fetch'));
      await tester.ensureVisible(fetch);
      await tester.pump();
      expect(tester.getSize(fetch).height, greaterThanOrEqualTo(48));
      expect(find.text("Needs this engine's package (from the PC)."), findsOneWidget);
      expect(find.text('The PC has no package of this engine for this avatar: train and export it on the PC (Settings > Voice).'), findsOneWidget, reason: 'Ultralight: the manifest names only FeatherTalk');
      await tester.tap(fetch);
      await tester.pump();
      for (var i = 0; i < 100 && hub.status(avatarEngines.firstWhere((e) => e.id == 'feathertalk')) != EngineStatus.notTested; i++) {
        await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
        await tester.pump();
      }
      final test = find.byKey(const ValueKey('engine-feathertalk-test'));
      await tester.pump();
      await tester.ensureVisible(test);
      await tester.pump();
      expect(tester.getSize(test).height, greaterThanOrEqualTo(48));
      expect(find.textContaining('Package on this phone:'), findsOneWidget);
      await tester.tap(test);
      for (var i = 0; i < 100 && !hub.personalReady('feathertalk'); i++) {
        await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
        await tester.pump();
      }
      expect(hub.personalReady('feathertalk'), isTrue);
      await tester.pump();
      await tester.pump();
      expect(find.text('Ready: the test measured 50.0 frames per second on this phone.'), findsOneWidget);
      expect(find.byKey(const ValueKey('engine-feathertalk-fetch')), findsNothing);
      expect(find.byKey(const ValueKey('engine-feathertalk-test')), findsNothing);
      await tester.ensureVisible(find.text('FeatherTalk'));
      await tester.pump();
      await tester.tap(find.text('FeatherTalk'));
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
      expect(settings.avatarMode, 'feathertalk');
    });
  }
}
