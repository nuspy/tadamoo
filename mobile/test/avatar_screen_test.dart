import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/avatar_hub.dart';
import 'package:basalt_mobile/src/avatar_screen.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';
import 'render_fakes.dart';
import 'secret_fakes.dart';

void main() {
  late FakeDevices devices;
  late FakeConnector connector;
  late AvatarController c;

  Future<FakeChannel> show(WidgetTester tester, {Size size = const Size(360, 640), double text = 1.0, double keyboard = 0, Map<String, Object> prefs = const {}, Map<String, dynamic>? ready, EngineHub? hub, RendererFactory? factory}) async {
    Texts.use('en');
    tester.view.physicalSize = Size(size.width * 3, size.height * 3);
    tester.view.devicePixelRatio = 3;
    tester.view.viewInsets = FakeViewPadding(bottom: keyboard * 3);
    addTearDown(tester.view.reset);
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', ...prefs});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    devices = FakeDevices();
    connector = FakeConnector();
    c = AvatarController(
      api: FakeGatewayApi(settings),
      settings: settings,
      devices: devices,
      client: GatewayClient(connector: connector.call, wait: (d) async {}),
      hub: hub == null ? null : EngineHub(settings: settings, store: hub.store),
      rendererFactory: factory,
    );
    addTearDown(c.dispose);
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(text)), child: child!),
      home: Scaffold(body: AvatarScreen(controller: c, active: true)),
    ));
    await tester.pump();
    await tester.pump();
    for (var i = 0; i < 100 && connector.channels.isEmpty; i++) {
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
    }
    connector.channel.say(ready ?? readyMessage);
    await tester.pump();
    await tester.pump();
    return connector.channel;
  }

  const brainDirect = ValueKey('brain-direct');
  const brainHarness = ValueKey('brain-harness');
  const routeBt = ValueKey('route-bluetooth');
  const routeSpeaker = ValueKey('route-speaker');
  const routeEarpiece = ValueKey('route-earpiece');
  const allChoices = [brainDirect, brainHarness, routeBt, routeSpeaker, routeEarpiece];

  for (final size in const [Size(360, 640), Size(390, 844), Size(1280, 800)]) {
    for (final text in const [1.0, 2.0]) {
      testWidgets('voice only, ${size.width.toInt()}x${size.height.toInt()} at text ${text}x: the controls are there, 48 dp at least, and nothing overflows', (tester) async {
        await show(tester, size: size, text: text);
        expect(tester.takeException(), isNull);
        expect(find.text('Connected to the PC.'), findsOneWidget);
        expect(find.textContaining('No avatar is loaded on the PC: voice only'), findsOneWidget);
        expect(find.text('Hold to talk'), findsOneWidget);
        expect(find.text('Text chat'), findsOneWidget);
        expect(find.byType(TextField), findsNothing, reason: 'the field exists only while the accordion is open');
        for (final k in allChoices) {
          final s = tester.getSize(find.byKey(k));
          expect(s.width, greaterThanOrEqualTo(48), reason: '$k');
          expect(s.height, greaterThanOrEqualTo(48), reason: '$k');
        }
        final bar = tester.getSize(find.byType(AnimatedContainer).last);
        expect(bar.height, greaterThanOrEqualTo(48));
        expect(bar.height, lessThan(72), reason: 'the bar is thinner than the old 72 dp button');
        expect(tester.getSize(find.byIcon(Icons.face)).height, greaterThan(0));
        final header = tester.getSize(find.ancestor(of: find.text('Text chat'), matching: find.byType(ConstrainedBox)).first);
        expect(header.height, greaterThanOrEqualTo(48));
        expect(tester.takeException(), isNull);
      });
    }
  }

  testWidgets('the settings of the avatar are no longer on the tab', (tester) async {
    await show(tester);
    expect(find.text('Wav2Lip on the phone'), findsNothing);
    expect(find.byType(SegmentedButton<String>), findsNothing);
    expect(find.byType(DropdownButtonFormField<String>), findsNothing);
  });

  testWidgets('every icon has a label for TalkBack, and the chosen one says so', (tester) async {
    final handle = tester.ensureSemantics();
    await show(tester);
    expect(tester.getSemantics(find.byKey(brainDirect)), isSemantics(label: 'Direct: fast light chat with the model', isSelected: true, isButton: true));
    expect(tester.getSemantics(find.byKey(brainHarness)), isSemantics(label: 'Harness: the agent with tools', isButton: true));
    expect(tester.getSemantics(find.byKey(routeBt)), isSemantics(label: 'Bluetooth headset (full duplex)', isButton: true));
    expect(tester.getSemantics(find.byKey(routeSpeaker)), isSemantics(label: 'Speakerphone', isSelected: true, value: 'half duplex'));
    expect(tester.getSemantics(find.byKey(routeEarpiece)), isSemantics(label: 'Phone microphone with earpiece'));
    expect(find.bySemanticsLabel('Avatar on. Tap for voice only.'), findsOneWidget);
    handle.dispose();
  });

  testWidgets('the accordion opens and closes: the avatar gets smaller, the field and the conversation appear only while it is open', (tester) async {
    final handle = tester.ensureSemantics();
    await show(tester);
    final stageClosed = tester.getRect(find.ancestor(of: find.byIcon(Icons.record_voice_over_outlined), matching: find.byType(Container)).last).height;
    expect(find.byType(TextField), findsNothing);
    expect(tester.getSemantics(find.text('Text chat')), isSemantics(isExpanded: false));

    await tester.tap(find.text('Text chat'));
    await tester.pump();
    expect(find.byType(TextField), findsOneWidget);
    expect(find.textContaining('Hold the button and talk'), findsOneWidget);
    expect(tester.getSemantics(find.text('Text chat')), isSemantics(isExpanded: true));
    final stageOpen = tester.getRect(find.ancestor(of: find.byIcon(Icons.record_voice_over_outlined), matching: find.byType(Container)).last).height;
    expect(stageOpen, lessThan(stageClosed), reason: 'the avatar area is restricted while the chat is open');
    expect(find.text('Hold to talk'), findsOneWidget, reason: 'the bar stays');
    expect(tester.takeException(), isNull);

    await tester.tap(find.text('Text chat'));
    await tester.pump();
    expect(find.byType(TextField), findsNothing);
    handle.dispose();
  });

  testWidgets('a typed sentence is sent from the open chat and shows in it', (tester) async {
    final ch = await show(tester);
    await tester.tap(find.text('Text chat'));
    await tester.pump();
    await tester.enterText(find.byType(TextField), 'what is the weather');
    await tester.tap(find.byIcon(Icons.send));
    await tester.pump();
    await tester.pump();
    expect(ch.last('text')!['text'], 'what is the weather');
    expect(find.text('what is the weather'), findsWidgets);
    expect(tester.widget<TextField>(find.byType(TextField)).controller!.text, isEmpty);
  });

  for (final text in const [1.0, 2.0]) {
    testWidgets('the keyboard is up with the chat open at text ${text}x: the field is still on the screen and nothing overflows', (tester) async {
      await show(tester, keyboard: 300, text: text);
      await tester.tap(find.text('Text chat'));
      await tester.pump();
      expect(tester.takeException(), isNull);
      await tester.tap(find.byType(TextField));
      await tester.pump();
      final field = tester.getRect(find.byType(TextField));
      expect(field.bottom, lessThanOrEqualTo(640 - 300 + 0.5), reason: 'the field is above the keyboard');
      expect(field.top, greaterThanOrEqualTo(0));
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('Direct and Harness are chosen with the two icons and the choice goes to the PC', (tester) async {
    final ch = await show(tester);
    await tester.tap(find.byKey(brainHarness));
    await tester.pump();
    await tester.pump();
    expect(ch.last('brain'), {'type': 'brain', 'brain': 'harness'});
    expect(c.settings.avatarBrain, 'harness');
    ch.say({'type': 'brain', 'brain': 'harness'});
    await tester.pump();
    final handle = tester.ensureSemantics();
    expect(tester.getSemantics(find.byKey(brainHarness)), isSemantics(isSelected: true));
    expect(tester.getSemantics(find.byKey(brainDirect)), isNot(isSemantics(isSelected: true)));
    handle.dispose();
    await tester.tap(find.byKey(brainDirect));
    await tester.pump();
    expect(ch.last('brain'), {'type': 'brain', 'brain': 'direct'});
  });

  testWidgets('the sound route is chosen with the icons; Bluetooth is only asked for after saying why, and a refusal keeps the old route', (tester) async {
    await show(tester, prefs: {'flutter.audioRoute': 'earpiece'});
    devices.fakeRouter.permission = false;
    devices.fakeRouter.grantWhenAsked = false;
    expect(c.route.selected.name, 'earpiece');
    expect(devices.fakeRouter.calls, ['start earpiece']);
    await tester.tap(find.byKey(routeSpeaker));
    await tester.pump();
    await tester.pump();
    expect(c.settings.audioRoute, 'speaker');
    expect(devices.fakeRouter.calls, contains('route speaker'));

    await tester.tap(find.byKey(routeBt));
    await tester.pumpAndSettle();
    expect(find.textContaining('Nearby devices'), findsOneWidget, reason: 'the reason is shown before Android asks');
    expect(devices.fakeRouter.calls, isNot(contains('request')));
    await tester.tap(find.text('Not now'));
    await tester.pumpAndSettle();
    expect(devices.fakeRouter.calls, isNot(contains('request')));
    expect(c.settings.audioRoute, 'speaker');

    await tester.tap(find.byKey(routeBt));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    expect(devices.fakeRouter.calls, contains('request'));
    expect(c.settings.audioRoute, 'speaker', reason: 'the permission was refused');
    expect(find.textContaining('Without Android\'s permission for nearby devices'), findsOneWidget);

    devices.fakeRouter.grantWhenAsked = true;
    await tester.tap(find.byKey(routeBt));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    expect(c.settings.audioRoute, 'bluetooth');
    final handle = tester.ensureSemantics();
    expect(tester.getSemantics(find.byKey(routeBt)), isSemantics(isSelected: true, value: 'full duplex'));
    handle.dispose();
  });

  testWidgets('the quick switch turns the avatar off: no picture area, the PC is told to stop the video, the controls stay', (tester) async {
    final ch = await show(tester, ready: {...readyMessage, 'avatar': true});
    expect(find.textContaining('Preparing the avatar on the PC'), findsOneWidget);
    expect(ch.kinds, contains('video_start'));
    await tester.tap(find.byIcon(Icons.face));
    await tester.pump();
    await tester.pump();
    expect(c.settings.avatarEnabled, isFalse);
    expect(ch.kinds, contains('video_stop'));
    expect(find.byIcon(Icons.face_retouching_off), findsOneWidget);
    expect(find.textContaining('Avatar off: voice only'), findsOneWidget);
    expect(find.byKey(const ValueKey('avatar-video')), findsNothing);
    expect(find.text('Hold to talk'), findsOneWidget);
    expect(find.text('Text chat'), findsOneWidget);
    await tester.tap(find.text('Text chat'));
    await tester.pump();
    expect(find.byType(TextField), findsOneWidget);
    expect(tester.takeException(), isNull);
    final starts = ch.kinds.where((k) => k == 'video_start').length;
    await tester.tap(find.byIcon(Icons.face_retouching_off));
    await tester.pump();
    await tester.pump();
    expect(c.settings.avatarEnabled, isTrue);
    expect(ch.kinds.where((k) => k == 'video_start').length, starts + 1, reason: 'switching it on asks for the video again');
    ch.say({'type': 'video_state', 'state': 'live'});
    await tester.pump();
  });

  testWidgets('the picture of the PC is shown when the avatar is loaded and the stream is live; before that the note says it is being prepared', (tester) async {
    final ch = await show(tester, ready: {...readyMessage, 'avatar': true});
    expect(find.textContaining('Preparing the avatar on the PC'), findsOneWidget);
    expect(find.byKey(const ValueKey('avatar-video')), findsNothing);
    ch.say({'type': 'video_state', 'state': 'live'});
    await tester.pump();
    await tester.pump();
    expect(find.byKey(const ValueKey('avatar-video')), findsOneWidget);
    expect(find.textContaining('Preparing the avatar'), findsNothing);
  });

  testWidgets('with nothing to draw on the phone and the PC saying none, the tab says it is voice only and asks for no video', (tester) async {
    final ch = await show(tester, prefs: {'flutter.avatarMode': 'wav2lip'}, ready: {...readyMessage, 'render_mode': 'none', 'render_modes': []});
    expect(find.textContaining('No avatar to show: voice only'), findsOneWidget);
    expect(ch.kinds, isNot(contains('video_start')));
    expect(find.text('Hold to talk'), findsOneWidget);
  });

  testWidgets('when the phone draws the avatar the tab shows it and not the video, and says what draws it and at what size', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final bare = await AppSettings.load(secrets: FakeSecretStore());
    final dir = Directory.systemTemp.createTempSync('basalt_tab');
    addTearDown(() => dir.deleteSync(recursive: true));
    late AvatarAssetStore store;
    await tester.runAsync(() async {
      final source = FakeAssetSource()..serve(FakeAvatarFiles('ada'));
      store = AvatarAssetStore(root: dir, source: source);
      await store.sync('ada');
    });
    addTearDown(store.dispose);
    final ch = await show(
      tester,
      prefs: {'flutter.avatarName': 'ada'},
      ready: {...readyMessage, 'avatar': true, 'render_mode': 'visemes', 'render_modes': ['visemes', 'video'], 'engine': 'visemes', 'resolution': 240},
      hub: EngineHub(settings: bare, store: store),
      factory: (mode, name, why) async => FakeRenderer(mode),
    );
    for (var i = 0; i < 100 && find.byKey(const ValueKey('renderer-visemes')).evaluate().isEmpty; i++) {
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 20)));
      await tester.pump();
    }
    expect(find.byKey(const ValueKey('renderer-visemes')), findsOneWidget);
    expect(find.text('Drawn by Visemes, 240 px.'), findsOneWidget);
    expect(find.byKey(const ValueKey('avatar-video')), findsNothing);
    expect(ch.kinds, isNot(contains('video_start')), reason: 'the video is the emergency only');
    expect(find.text('Hold to talk'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('holding the bar talks and releasing sends', (tester) async {
    final ch = await show(tester);
    final gesture = await tester.startGesture(tester.getCenter(find.text('Hold to talk')));
    await tester.pump();
    await tester.pump();
    expect(ch.last('start'), isNotNull);
    expect(find.text('Release to send'), findsOneWidget);
    await gesture.up();
    await tester.runAsync(() => pumpEventQueue());           // cancelling the microphone's stream completes outside the test's fake clock
    await tester.pump();
    expect(ch.last('end'), isNotNull);
  });

  testWidgets('voice not loaded on the PC: the screen says what is missing and the bar does nothing', (tester) async {
    final ch = await show(tester, ready: {...readyMessage, 'voice': false, 'missing': ['stt', 'tts']});
    expect(find.textContaining('speech recognition, speech synthesis'), findsOneWidget);
    await tester.tap(find.text('Hold to talk'));
    await tester.pump();
    expect(ch.kinds, isNot(contains('start')));
  });

  testWidgets('the continuation the PC offers after an interruption is a line with a button; taking it tells the PC', (tester) async {
    final ch = await show(tester);
    ch.say({'type': 'resume_offer', 'text': 'the second step of the recipe'});
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('the second step of the recipe'), findsOneWidget);
    expect(tester.getSize(find.widgetWithText(TextButton, 'Continue')).height, greaterThanOrEqualTo(48));
    await tester.tap(find.widgetWithText(TextButton, 'Continue'));
    await tester.pump();
    expect(ch.last('resume'), {'type': 'resume', 'accept': true});
    expect(find.textContaining('the second step of the recipe'), findsNothing);
  });

  testWidgets('in Italian', (tester) async {
    await show(tester);
    Texts.use('it');
    await tester.pump();
    c.client.clearProblem();
    await tester.pump();
    expect(find.text('Tieni premuto per parlare'), findsOneWidget);
    expect(find.text('Chat di testo'), findsOneWidget);
  });
}
