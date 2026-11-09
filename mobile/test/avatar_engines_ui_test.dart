import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:basalt_mobile/src/audio_route.dart';
import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_hub.dart';
import 'package:basalt_mobile/src/avatar_settings.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/wav2lip_engine.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';
import 'secret_fakes.dart';

void main() {
  late Directory dir;
  late AppSettings settings;
  late FakeAssetSource source;
  late AvatarAssetStore store;
  late FakeWav2LipBackend backend;
  late EngineHub hub;

  Future<void> show(WidgetTester tester, {Size size = const Size(360, 640), double text = 1.0, Map<String, Object> prefs = const {}, bool lang = false}) async {
    Texts.use(lang ? 'it' : 'en');
    tester.view.physicalSize = Size(size.width * 3, size.height * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    SharedPreferences.setMockInitialValues(prefs);
    settings = await AppSettings.load(secrets: FakeSecretStore());
    dir = Directory.systemTemp.createTempSync('basalt_ui');
    source = FakeAssetSource()..modelBytes = Uint8List.fromList(utf8.encode('the model'));
    store = AvatarAssetStore(root: dir, source: source, modelSha: sha(utf8.encode('the model')), modelBytes: 9);
    backend = FakeWav2LipBackend();
    hub = EngineHub(settings: settings, store: store, backend: backend);
    source.serve(FakeAvatarFiles('ada'));
    await tester.runAsync(hub.refresh);
    final route = AudioRouteController(router: FakeRouter(), settings: settings);
    addTearDown(() async {
      route.dispose();
      hub.dispose();
      store.dispose();
      await Future<void>.delayed(const Duration(milliseconds: 30));
      dir.deleteSync(recursive: true);
    });
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(text)), child: child!),
      home: Scaffold(
        body: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: AvatarVoiceSettings(settings: settings, route: route, hub: hub, loadPersonas: () async => const []),
        ),
      ),
    ));
    await tester.pump();
    await tester.pump();
  }

  /// Real file and zip work happens outside the test's clock.
  Future<void> settle(WidgetTester tester, [bool Function()? done]) async {
    for (var i = 0; i < 150 && !(done?.call() ?? i >= 10); i++) {
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
    }
    await tester.pump();
    await tester.pump();
  }

  Future<void> press(WidgetTester tester, Finder f) async {
    await tester.ensureVisible(f);
    await tester.pump();
    await tester.tap(f);
    await tester.pump();
  }

  testWidgets('before anything is on the phone each engine says what it needs, honestly', (tester) async {
    await show(tester);
    expect(find.text('Needs the avatar\'s files: open the Avatar tab once so that the PC names the avatar.'), findsNWidgets(1), reason: 'the visemes');
    expect(find.text('Needs the model (36.7 MB).'), findsOneWidget);
    expect(find.text('Not available: this build has no runtime for it.'), findsNWidgets(2), reason: 'Ultralight and FeatherTalk: this hub has no runtime for them');
    expect(find.text('Model of 36.7 MB (36,664,106 bytes) from the PC'), findsOneWidget);
    expect(find.text('Package trained on the PC for your person (the size shows once downloaded)'), findsNWidgets(2), reason: 'Ultralight and FeatherTalk');
    expect(find.textContaining('About 5 MB'), findsNothing, reason: 'no invented sizes');
    expect(find.text('Download the model (36.7 MB)'), findsOneWidget);
    expect(find.text('Download the avatar\'s files'), findsNothing, reason: 'the avatar is not named yet');
  });

  testWidgets('with the avatar named, the files are downloaded from the chooser and the visemes become ready, with their size', (tester) async {
    await show(tester, prefs: {'flutter.avatarName': 'ada'});
    expect(find.text('Needs the avatar\'s files (from the PC).'), findsOneWidget, reason: 'the visemes; Wav2Lip needs its model first');
    await press(tester, find.byKey(const ValueKey('engine-visemes-fetch')));
    await settle(tester, () => hub.visemesReady);
    expect(hub.visemesReady, isTrue);
    expect(find.byKey(const ValueKey('engine-visemes-fetch')), findsNothing);
    expect(find.textContaining('Files on this phone:'), findsOneWidget);
    expect(find.text('Ready on this phone.'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('Wav2Lip: download the model, then the test, and only then it is ready, with the speed that was measured', (tester) async {
    await show(tester, prefs: {'flutter.avatarName': 'ada'});
    await press(tester, find.byKey(const ValueKey('engine-visemes-fetch')));
    await settle(tester, () => hub.visemesReady);
    await press(tester, find.byKey(const ValueKey('engine-wav2lip-fetch')));
    await settle(tester, () => store.modelStage == ModelStage.ready);
    expect(store.modelStage, ModelStage.ready);
    expect(find.text('Test not run: use the avatar test below.'), findsOneWidget);
    expect(find.text('Needs the model (36.7 MB).'), findsNothing);

    await press(tester, find.byKey(const ValueKey('avatar-test')));
    await settle(tester, () => hub.lastTest != null);
    expect(backend.calls.last, 'selfTest true');
    final report = tester.widget<SelectableText>(find.byKey(const ValueKey('avatar-test-report'))).data!;
    expect(report, contains('Result: passed'));
    expect(report, contains('Wav2LipBench: 33.0 ms per frame, 30.0 frames/s'));
    expect(find.text('Ready: the test measured 30.0 frames per second on this phone.'), findsOneWidget);
    expect(settings.wav2lipPassed, store.modelSha);
  });

  testWidgets('a test that fails says so on the engine and in the report, and Wav2Lip is not ready', (tester) async {
    await show(tester, prefs: {'flutter.avatarName': 'ada'});
    await tester.runAsync(() async {
      await hub.fetchAssets('ada');
      await hub.downloadModel();
    });
    await tester.pump();
    backend.testResult = const Wav2LipTestResult(ok: false, reason: 'output_unlike_input', steps: [Wav2LipStep('output', false, 'differs by 0.4')]);
    await tester.pump();
    await press(tester, find.byKey(const ValueKey('avatar-test')));
    await settle(tester);
    expect(find.text('Test failed (output_unlike_input). The details are in the avatar test.'), findsOneWidget);
    final report = tester.widget<SelectableText>(find.byKey(const ValueKey('avatar-test-report'))).data!;
    expect(report, contains('failed (output_unlike_input)'));
    expect(report, contains('FAIL output: differs by 0.4'));
    expect(hub.wav2lipReady, isFalse);
  });

  testWidgets('the test with no model says what is missing instead of running', (tester) async {
    await show(tester);
    await press(tester, find.byKey(const ValueKey('avatar-test')));
    await settle(tester);
    expect(backend.calls, isEmpty);
    expect(tester.widget<SelectableText>(find.byKey(const ValueKey('avatar-test-report'))).data, contains('failed (no_model)'));
  });

  testWidgets('a model download that fails says why on the engine, and the button is there to try again', (tester) async {
    await show(tester);
    source.modelMissing = true;
    await press(tester, find.byKey(const ValueKey('engine-wav2lip-fetch')));
    await settle(tester, () => store.modelStage == ModelStage.failed);
    expect(find.text('The model was not downloaded (not_installed).'), findsOneWidget);
    expect(find.byKey(const ValueKey('engine-wav2lip-fetch')), findsOneWidget);
  });

  testWidgets('a build with no runtime for Wav2Lip says so', (tester) async {
    await show(tester);
    backend.runtime = false;
    await tester.runAsync(hub.refresh);
    await tester.pump();
    expect(find.text('Not available: this build has no runtime for it.'), findsNWidgets(3), reason: 'Wav2Lip, and the two personalised engines which this hub has no runtime for');
    expect(find.byKey(const ValueKey('engine-wav2lip-fetch')), findsNothing);
  });

  for (final size in const [Size(360, 640), Size(390, 844)]) {
    testWidgets('${size.width.toInt()}x${size.height.toInt()} at text 2x with the new lines and buttons: no overflow, 48 dp at least', (tester) async {
      await show(tester, size: size, text: 2.0, prefs: {'flutter.avatarName': 'ada'});
      for (final k in const [ValueKey('engine-visemes-fetch'), ValueKey('engine-wav2lip-fetch'), ValueKey('avatar-test')]) {
        await tester.ensureVisible(find.byKey(k));
        await tester.pump();
        expect(tester.getSize(find.byKey(k)).height, greaterThanOrEqualTo(48), reason: '$k');
      }
      await press(tester, find.byKey(const ValueKey('avatar-test')));
      await settle(tester);
      await tester.ensureVisible(find.byKey(const ValueKey('avatar-test-copy')));
      await tester.pump();
      expect(tester.getSize(find.byKey(const ValueKey('avatar-test-copy'))).height, greaterThanOrEqualTo(48));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('in Italian', (tester) async {
    await show(tester, lang: true);
    expect(find.text('Prova dell\'avatar'), findsWidgets);
    expect(find.text('Serve il modello (36,7 MB).'), findsOneWidget);
    expect(find.text('Scarica il modello (36,7 MB)'), findsOneWidget);
  });
}
