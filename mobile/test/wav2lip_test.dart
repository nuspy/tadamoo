import 'dart:io';
import 'dart:ui' as ui;

import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/wav2lip_engine.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('the self-test result', () {
    test('it passes only when the model ran and the speed reaches the minimum', () {
      final fast = Wav2LipTestResult.fromMap({'ok': true, 'msPerFrame': 40.0, 'threads': 4, 'frames': 40, 'steps': [{'name': 'model', 'ok': true, 'detail': 'loaded'}]});
      expect(fast.ok, isTrue);
      expect(fast.fps, closeTo(25, 1e-9));
      final slow = Wav2LipTestResult.fromMap({'ok': true, 'msPerFrame': 200.0, 'threads': 4, 'frames': 40});
      expect(slow.ok, isFalse);
      expect(slow.reason, 'too_slow');
      final broken = Wav2LipTestResult.fromMap({'ok': false, 'reason': 'bad_io', 'steps': [{'name': 'model', 'ok': false, 'detail': 'expected a mel input'}]});
      expect(broken.ok, isFalse);
      expect(broken.reason, 'bad_io');
    });

    test('the text says the result, each step, the benchmark and what the test cannot judge', () {
      final r = Wav2LipTestResult.fromMap({
        'ok': true,
        'msPerFrame': 33.3,
        'threads': 4,
        'frames': 40,
        'steps': [
          {'name': 'model', 'ok': true, 'detail': 'loaded'},
          {'name': 'output', 'ok': false, 'detail': 'differs'},
        ],
      });
      final t = r.toText();
      expect(t, contains('Result: passed'));
      expect(t, contains('ok   model: loaded'));
      expect(t, contains('FAIL output: differs'));
      expect(t, contains('Wav2LipBench: 33.3 ms per frame, 30.0 frames/s over 40 frames, 4 threads'));
      expect(t, contains('cannot tell whether the lips look right'));
      expect(const Wav2LipTestResult(ok: false, reason: 'no_model').toText(), contains('failed (no_model)'));
    });
  });

  group('the Kotlin channels', () {
    const calls = MethodChannel('app.basalt/wav2lip');
    const events = EventChannel('app.basalt/wav2lip/events');
    final log = <MethodCall>[];
    var answer = <String, Object?>{};

    setUp(() {
      log.clear();
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(calls, (call) async {
        log.add(call);
        switch (call.method) {
          case 'available':
            return true;
          case 'load':
          case 'selfTest':
            return answer;
          case 'start':
            return 7;
        }
        return null;
      });
    });

    tearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(calls, null);
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockStreamHandler(events, null);
    });

    test('the calls carry what the Kotlin side needs and the answers become results', () async {
      final b = ChannelWav2LipBackend();
      expect(await b.available(), isTrue);
      answer = {'ok': true};
      expect((await b.load(model: '/m.onnx', face: '/f.png')).ok, isTrue);
      expect(log.last.arguments, {'model': '/m.onnx', 'face': '/f.png'});
      answer = {'ok': false, 'reason': 'bad_io', 'detail': 'shapes'};
      final failed = await b.load(model: '/m.onnx', face: '/f.png');
      expect((failed.ok, failed.reason, failed.detail), (false, 'bad_io', 'shapes'));
      answer = {'ok': true, 'msPerFrame': 20.0, 'threads': 2, 'frames': 40, 'steps': []};
      final test = await b.selfTest(model: '/m.onnx', face: null, fps: 25);
      expect(test.ok, isTrue);
      expect(log.last.arguments, {'model': '/m.onnx', 'face': null, 'fps': 25});
      expect(await b.start(24000, 25), 7);
      expect(log.last.arguments, {'rate': 24000, 'fps': 25});
      b.feed(Uint8List.fromList([1, 2, 3, 4]));
      await b.finish();
      await b.stop();
      await b.unload();
      await pumpEventQueue();
      expect(log.map((c) => c.method).toList().sublist(log.length - 4), ['feed', 'finish', 'stop', 'unload']);
    });

    test('a failure of the runtime is a result, not an exception', () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(calls, (call) async => throw PlatformException(code: 'x', message: 'no library'));
      final b = ChannelWav2LipBackend();
      final l = await b.load(model: 'm', face: 'f');
      expect((l.ok, l.reason), (false, 'onnx_load_failed'));
      expect((await b.selfTest(fps: 25)).reason, 'no library');
    });

    test('without the Kotlin side (not Android) nothing is available', () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(calls, null);
      final b = ChannelWav2LipBackend();
      expect(await b.available(), isFalse);
      expect((await b.load(model: 'm', face: 'f')).reason, 'no_runtime');
      expect((await b.selfTest(fps: 25)).reason, 'no_runtime');
      expect(await b.start(16000, 25), 0);
    });

    test('frames, the thermal status and failures come back as events; what is not one is ignored', () async {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockStreamHandler(
        events,
        MockStreamHandler.inline(onListen: (args, sink) {
          sink.success({'k': 'frame', 'g': 3, 't': 40, 'w': 96, 'h': 96, 'rgba': Uint8List(4)});
          sink.success({'k': 'thermal', 'level': 2});
          sink.success({'k': 'error', 'why': 'bad_io'});
          sink.success({'k': 'unknown'});
          sink.success('junk');
          sink.success({'k': 'frame', 'g': 3, 't': 80});
        }),
      );
      final got = await ChannelWav2LipBackend().events.take(3).toList();
      final f = (got[0] as Wav2LipFrameEvent).frame;
      expect((f.gen, f.tMs, f.w, f.h), (3, 40, 96, 96));
      expect((got[1] as Wav2LipThermalEvent).level, 2);
      expect((got[2] as Wav2LipErrorEvent).why, 'bad_io');
    });
  });

  group('the files of the mode', () {
    testWidgets('the photo, the face path and where the mouth sits are read from the manifest\'s files', (tester) async {
      final dir = Directory.systemTemp.createTempSync('basalt_w2l');
      addTearDown(() => dir.deleteSync(recursive: true));
      late ui.Image img;
      await tester.runAsync(() async {
        img = await tinyImage();
        final source = FakeAssetSource()..serve(FakeAvatarFiles('ada'));
        final store = AvatarAssetStore(root: dir, source: source);
        await store.sync('ada');
        final p = await Wav2LipPack.load(store, 'ada', decoder: (b) async => img);
        expect(p, isNotNull);
        expect(p!.mouth, const ui.Rect.fromLTWH(20, 60, 40, 20));
        expect(p.inv, [0.5, 0, 10, 0, 0.5, 4]);
        expect(p.facePath, endsWith('wav2lip/face.png'));
        expect(p.posterSize, const ui.Size(40, 30));
        File(store.pathOf('ada', 'wav2lip/box.json')).writeAsStringSync('{"inv": [1], "mouth": {}}');
        expect(await Wav2LipPack.load(store, 'ada', decoder: (b) async => img), isNull);
        File(store.pathOf('ada', 'wav2lip/box.json')).writeAsStringSync('{"inv": [1,0,0,0,1,0], "mouth": {"x": 1, "y": 1, "w": 0, "h": 5}}');
        expect(await Wav2LipPack.load(store, 'ada', decoder: (b) async => img), isNull, reason: 'a mouth with no width');
        store.dispose();
      });
    });
  });

  group('the renderer', () {
    late FakeWav2LipBackend backend;
    late FakePlayer player;
    late Wav2LipPack pack;
    late Map<int, ui.Image> pics;
    var clock = 0;

    Future<Wav2LipRenderer> make(WidgetTester tester) async {
      backend = FakeWav2LipBackend();
      player = FakePlayer();
      clock = 0;
      await tester.runAsync(() async {
        final poster = await tinyImage(40, 30);
        pack = Wav2LipPack(poster: poster, posterSize: const ui.Size(40, 30), inv: const [0.5, 0, 10, 0, 0.5, 4], mouth: const ui.Rect.fromLTWH(20, 60, 40, 20), facePath: '/f.png');
        pics = {for (final t in [0, 40, 80, 120, 160, 1000]) t: await tinyImage(2, 2, ui.Color(0xFF000000 + t))};
      });
      final r = Wav2LipRenderer(backend: backend, pack: pack, player: player, leadMs: 0, now: () => clock, toImage: (f) async => pics[f.tMs]!);
      addTearDown(r.dispose);
      return r;
    }

    Future<void> settle(WidgetTester tester) async {
      await tester.runAsync(() => pumpEventQueue());
      await tester.pump();
    }

    testWidgets('a reply starts the model at the voice\'s rate, the voice is fed to it, and the end of the voice ends it', (tester) async {
      final r = await make(tester);
      player.queuedMs = 0;
      r.audioStart(24000);
      r.audio(Uint8List(960));
      r.audio(Uint8List(960));
      r.audioEnd();
      await settle(tester);
      expect(backend.calls, ['start 24000 25', 'finish']);
      expect(backend.fed.length, 2);
      r.audioStart(24000);
      r.audioStart(24000);
      await settle(tester);
      expect(backend.calls.where((c) => c.startsWith('start')).length, 2, reason: 'a reply that is already open is not started again');
    });

    testWidgets('a frame is shown when the voice has got to its time, on the clock of the voice, and older ones are dropped', (tester) async {
      final r = await make(tester);
      player.queuedMs = 500;
      r.audioStart(24000);
      await settle(tester);
      for (final t in [0, 40, 80, 120]) {
        backend.frame(1, t);
      }
      await settle(tester);
      player.queuedMs = 1500;

      player.playedMs = 400;
      r.tick();
      expect(r.scene!.layers, isEmpty, reason: 'the voice has not reached the first frame');
      player.playedMs = 520;
      r.tick();
      expect(r.scene!.layers.single.image, same(pics[0]));
      player.playedMs = 545;
      r.tick();
      expect(r.scene!.layers.single.image, same(pics[40]));
      player.playedMs = 700;
      r.tick();
      expect(r.scene!.layers.single.image, same(pics[120]), reason: 'the latest frame that is due');
      expect(r.framesShown, 3);
      final layer = r.scene!.layers.single;
      expect(layer.clip, const ui.Rect.fromLTWH(20, 60, 40, 20));
      expect(layer.inv, [0.5, 0, 10, 0, 0.5, 4]);
    });

    testWidgets('a frame is not shown once the voice is over', (tester) async {
      final r = await make(tester);
      r.audioStart(24000);
      await settle(tester);
      backend.frame(1, 0);
      await settle(tester);
      player.queuedMs = 1000;
      player.playedMs = 100;
      r.tick();
      expect(r.scene!.layers, hasLength(1));
      player.playedMs = 1000;
      r.tick();
      expect(r.scene!.layers, isEmpty);
    });

    testWidgets('an interrupt drops the frames, stops the model and shows the photo alone; late frames of the old reply are ignored', (tester) async {
      final r = await make(tester);
      r.audioStart(24000);
      await settle(tester);
      backend.frame(1, 0);
      backend.frame(1, 40);
      await settle(tester);
      player.queuedMs = 1000;
      player.playedMs = 100;
      r.tick();
      expect(r.scene!.layers, hasLength(1));
      r.flush();
      expect(backend.calls.last, 'stop');
      expect(r.scene!.layers, isEmpty);
      backend.frame(1, 80);
      await settle(tester);
      player.playedMs = 200;
      r.tick();
      expect(r.scene!.layers, isEmpty);
      r.audioStart(24000);
      await settle(tester);
      expect(backend.calls.where((c) => c.startsWith('start')).length, 2, reason: 'the next reply starts normally');
    });

    testWidgets('a model that falls behind the voice for too long is given up with too_slow', (tester) async {
      final r = await make(tester);
      String? why;
      r.onFailed = (w) => why = w;
      player.queuedMs = 0;
      r.audioStart(24000);
      await settle(tester);
      backend.frame(1, 0);
      await settle(tester);
      player.queuedMs = 10000;
      player.playedMs = 1000;
      clock = 0;
      r.tick();
      expect(why, isNull, reason: 'a moment of delay is tolerated');
      clock = 1000;
      player.playedMs = 2000;
      r.tick();
      expect(why, isNull);
      clock = 2000;
      player.playedMs = 3000;
      r.tick();
      expect(why, 'too_slow');
      expect(backend.calls, contains('stop'));
    });

    testWidgets('a model that catches up in time is not given up', (tester) async {
      final r = await make(tester);
      String? why;
      r.onFailed = (w) => why = w;
      r.audioStart(24000);
      await settle(tester);
      backend.frame(1, 0);
      await settle(tester);
      player.queuedMs = 10000;
      player.playedMs = 1000;
      r.tick();
      clock = 1000;
      backend.frame(1, 1000);
      await settle(tester);
      player.playedMs = 1100;
      r.tick();
      clock = 3000;
      player.playedMs = 1200;
      r.tick();
      expect(why, isNull);
    });

    testWidgets('a phone that is too hot makes it give up with thermal; a warm one does not', (tester) async {
      final r = await make(tester);
      String? why;
      r.onFailed = (w) => why = w;
      backend.controller.add(Wav2LipThermalEvent(2));
      await settle(tester);
      expect(why, isNull);
      backend.controller.add(Wav2LipThermalEvent(thermalLimit));
      await settle(tester);
      expect(why, 'thermal');
    });

    testWidgets('a failure of the runtime is reported with its reason, once', (tester) async {
      final r = await make(tester);
      final why = <String>[];
      r.onFailed = why.add;
      backend.controller.add(Wav2LipErrorEvent('inference_failed'));
      backend.controller.add(Wav2LipErrorEvent('bad_io'));
      await settle(tester);
      expect(why, ['inference_failed']);
    });
  });
}
