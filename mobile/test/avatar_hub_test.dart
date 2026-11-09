import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_engines.dart';
import 'package:basalt_mobile/src/avatar_hub.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/wav2lip_engine.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'render_fakes.dart';
import 'secret_fakes.dart';

void main() {
  late Directory dir;
  late AppSettings settings;
  late FakeAssetSource source;
  late AvatarAssetStore store;
  late FakeWav2LipBackend backend;
  late EngineHub hub;

  Future<void> make([Map<String, Object> prefs = const {}]) async {
    SharedPreferences.setMockInitialValues(prefs);
    settings = await AppSettings.load(secrets: FakeSecretStore());
    dir = Directory.systemTemp.createTempSync('basalt_hub');
    source = FakeAssetSource()..modelBytes = Uint8List.fromList(utf8.encode('the model'));
    store = AvatarAssetStore(root: dir, source: source, modelSha: sha(utf8.encode('the model')), modelBytes: 9);
    backend = FakeWav2LipBackend();
    hub = EngineHub(settings: settings, store: store, backend: backend);
  }

  tearDown(() {
    hub.dispose();
    store.dispose();
    dir.deleteSync(recursive: true);
  });

  AvatarEngine engine(String id) => avatarEngines.firstWhere((e) => e.id == id);

  Future<void> withAssets({bool wav2lip = true}) async {
    source.serve(FakeAvatarFiles('ada', wav2lip: wav2lip));
    expect(await hub.fetchAssets('ada'), isTrue);
  }

  group('what the app declares', () {
    test('with nothing on the phone only the PC\'s video is declared, and no engine is stated', () async {
      await make();
      await hub.refresh();
      expect(hub.plan().modes, ['video']);
      expect(hub.plan().fields, {'render_modes': ['video']});
    });

    test('the visemes are declared once the avatar\'s files are whole and verified, first, with the video last and the person\'s engine and height', () async {
      await make();
      expect(hub.visemesReady, isFalse);
      await withAssets();
      expect(hub.visemesReady, isTrue);
      final p = hub.plan();
      expect(p.modes, ['visemes', 'video']);
      expect(p.fields, {'render_modes': ['visemes', 'video'], 'engine': 'visemes', 'resolution': 240});
    });

    test('the avatar named last is the one whose files count, and the name is kept', () async {
      await make();
      await withAssets();
      expect(settings.avatarName, 'ada');
      expect(hub.assetBytes(), greaterThan(0));
      await settings.setAvatarName('someone');
      expect(hub.visemesReady, isFalse, reason: 'no files for that avatar');
      expect(hub.assetBytes(), 0);
    });

    test('the choice of the video, or the avatar being off, declares only the video', () async {
      await make({'flutter.avatarMode': 'video'});
      await withAssets();
      expect(hub.plan().modes, ['video']);
      await settings.setAvatarMode('visemes');
      expect(hub.plan().modes, ['visemes', 'video']);
      await settings.setAvatarEnabled(false);
      expect(hub.plan().modes, ['video']);
    });

    test('Wav2Lip is never declared before the model is on the phone, the runtime is there and the self-test passed here with this model', () async {
      await make({'flutter.avatarMode': 'wav2lip'});
      await withAssets();
      await hub.refresh();
      expect(hub.plan().modes, ['visemes', 'video'], reason: 'wav2lip chosen, but nothing else is in place');
      expect(await hub.downloadModel(), isTrue);
      expect(hub.plan().modes, ['visemes', 'video'], reason: 'the model is there, the test was not run');
      backend.testResult = const Wav2LipTestResult(ok: false, reason: 'bad_io');
      await hub.runTest();
      expect(hub.plan().modes, ['visemes', 'video']);
      backend.testResult = const Wav2LipTestResult(ok: true, fps: 30, msPerFrame: 33, frames: 40);
      await hub.runTest();
      expect(hub.plan().modes, ['wav2lip', 'visemes', 'video']);
      expect(hub.plan().engine, 'wav2lip_gan96');
      expect(hub.plan().resolution, 0, reason: '240 is a height Wav2Lip does not offer: the PC keeps its own');
      await settings.setAvatarResolution(144);
      expect(hub.plan().resolution, 144);
    });

    test('with the visemes chosen, Wav2Lip is not declared even when it is ready: it stays the default engine until the person chooses', () async {
      await make();
      await withAssets();
      await hub.refresh();
      await hub.downloadModel();
      await hub.runTest();
      expect(hub.wav2lipReady, isTrue);
      expect(hub.plan().modes, ['visemes', 'video']);
    });

    test('a test that passed for another model does not count, a new model has to pass again, and a runtime that is gone declares nothing', () async {
      await make({'flutter.avatarMode': 'wav2lip', 'flutter.wav2lipPassed': 'f' * 64});
      await withAssets();
      await hub.downloadModel();
      expect(hub.wav2lipReady, isFalse);
      await hub.refresh();
      await hub.runTest();
      expect(hub.wav2lipReady, isTrue);
      await hub.downloadModel();
      expect(settings.wav2lipPassed, '', reason: 'a downloaded file has not passed');
      expect(hub.wav2lipReady, isFalse);
      await hub.runTest();
      backend.runtime = false;
      await hub.refresh();
      expect(hub.wav2lipReady, isFalse);
    });

    test('a mode that failed on this phone is left out until it passes the test again', () async {
      await make({'flutter.avatarMode': 'wav2lip'});
      await withAssets();
      await hub.refresh();
      await hub.downloadModel();
      await hub.runTest();
      expect(hub.plan().modes.first, 'wav2lip');
      hub.suppress('wav2lip');
      expect(hub.plan().modes, ['visemes', 'video']);
      hub.suppress('visemes');
      expect(hub.plan().modes, ['video']);
      await hub.runTest();
      expect(hub.plan().modes, ['wav2lip', 'video'], reason: 'the test brought Wav2Lip back, the visemes stay out');
    });
  });

  group('the test', () {
    test('it needs the model; without the runtime or the model it says which, and runs nothing', () async {
      await make();
      await hub.refresh();
      expect((await hub.runTest()).reason, 'no_model');
      backend.runtime = false;
      await hub.refresh();
      expect((await hub.runTest()).reason, 'no_runtime');
      expect(backend.calls, isEmpty);
    });

    test('it uses the avatar\'s face when its files are here and its own otherwise, and keeps the speed it measured', () async {
      await make();
      await hub.refresh();
      await hub.downloadModel();
      await hub.runTest();
      expect(backend.calls.last, 'selfTest false');
      await withAssets();
      await hub.runTest();
      expect(backend.calls.last, 'selfTest true');
      expect(settings.wav2lipFps, 30);
      expect(settings.wav2lipMs, 33);
      expect(hub.lastTest!.ok, isTrue);
    });

    test('a test that throws is a failed test', () async {
      await make();
      await hub.refresh();
      await hub.downloadModel();
      backend.testResult = const Wav2LipTestResult(ok: false, reason: 'onnx_load_failed');
      final r = await hub.runTest();
      expect(r.ok, isFalse);
      expect(settings.wav2lipPassed, '');
    });
  });

  group('how an engine stands, for the chooser', () {
    test('the personalised engines without a runtime in this build say so; the video is always ready', () async {
      await make();
      expect(hub.status(engine('ultralight')), EngineStatus.noRuntime);
      expect(hub.status(engine('feathertalk')), EngineStatus.noRuntime);
      expect(hub.status(engine('video')), EngineStatus.ready);
    });

    test('visemes: they need the files, then they are ready', () async {
      await make();
      await hub.refresh();
      expect(hub.status(engine('visemes')), EngineStatus.needsAssets);
      await withAssets();
      expect(hub.status(engine('visemes')), EngineStatus.ready);
    });

    test('wav2lip: no runtime, then the model, then the files, then the test, then ready', () async {
      await make();
      backend.runtime = false;
      await hub.refresh();
      expect(hub.status(engine('wav2lip')), EngineStatus.noRuntime);
      backend.runtime = true;
      await hub.refresh();
      expect(hub.status(engine('wav2lip')), EngineStatus.needsModel);
      await hub.downloadModel();
      expect(hub.status(engine('wav2lip')), EngineStatus.needsAssets);
      await withAssets();
      expect(hub.status(engine('wav2lip')), EngineStatus.notTested);
      backend.testResult = const Wav2LipTestResult(ok: false, reason: 'bad_io');
      await hub.runTest();
      expect(hub.status(engine('wav2lip')), EngineStatus.failed);
      backend.testResult = const Wav2LipTestResult(ok: true, fps: 20, msPerFrame: 50, frames: 40);
      await hub.runTest();
      expect(hub.status(engine('wav2lip')), EngineStatus.ready);
    });

    test('without a store or a runtime object nothing can be drawn here', () async {
      await make();
      final bare = EngineHub(settings: settings);
      await bare.refresh();
      expect(bare.status(engine('visemes')), EngineStatus.noRuntime);
      expect(bare.status(engine('wav2lip')), EngineStatus.noRuntime);
      expect(bare.plan().modes, ['video']);
      expect(await bare.fetchAssets('ada'), isFalse);
      expect(await bare.downloadModel(), isFalse);
      expect((await bare.runTest()).reason, 'no_runtime');
      bare.dispose();
    });
  });
}
