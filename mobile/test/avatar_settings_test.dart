import 'package:basalt_mobile/src/audio_route.dart';
import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/avatar_engines.dart';
import 'package:basalt_mobile/src/avatar_settings.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';

void main() {
  late AppSettings settings;
  late FakeRouter router;
  late AudioRouteController route;

  const personas = [
    {'slug': 'bella', 'name': 'Bella'},
    {'slug': 'otto', 'name': 'Otto'},
  ];

  Future<void> show(WidgetTester tester, {Size size = const Size(360, 640), double text = 1.0, Map<String, Object> prefs = const {}, bool lang = false}) async {
    Texts.use(lang ? 'it' : 'en');
    tester.view.physicalSize = Size(size.width * 3, size.height * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    SharedPreferences.setMockInitialValues(prefs);
    settings = await AppSettings.load();
    router = FakeRouter();
    route = AudioRouteController(router: router, settings: settings);
    addTearDown(route.dispose);
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      builder: (context, child) => MediaQuery(data: MediaQuery.of(context).copyWith(textScaler: TextScaler.linear(text)), child: child!),
      home: Scaffold(
        body: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: AvatarVoiceSettings(settings: settings, route: route, loadPersonas: () async => personas),
        ),
      ),
    ));
    await tester.pump();
    await tester.pump();
  }

  Future<void> press(WidgetTester tester, Finder f) async {
    await tester.ensureVisible(f);
    await tester.pump();
    await tester.tap(f);
    await tester.pump();
    await tester.pump();
  }

  Future<void> reveal(WidgetTester tester, Finder f) async {
    await tester.scrollUntilVisible(f, 150, scrollable: find.byType(Scrollable).first);
    await tester.pump();
  }

  for (final size in const [Size(360, 640), Size(390, 844)]) {
    for (final text in const [1.0, 2.0]) {
      testWidgets('${size.width.toInt()}x${size.height.toInt()} at text ${text}x: the whole section scrolls without overflow and its controls are 48 dp at least', (tester) async {
        await show(tester, size: size, text: text);
        await press(tester, find.text('Audio test'));
        final keys = <Key>[
          const ValueKey('avatar-enabled'),
          for (final e in avatarEngines) ValueKey('engine-${e.id}'),
          for (final r in AudioRoute.values) ValueKey('setroute-${r.name}'),
          const ValueKey('full-duplex'),
        ];
        for (final k in keys) {
          await reveal(tester, find.byKey(k));
          expect(tester.getSize(find.byKey(k)).height, greaterThanOrEqualTo(48), reason: '$k');
        }
        for (final t in [find.byType(DropdownButtonFormField<int>), find.byType(DropdownButtonFormField<AvatarQuality>), find.byType(DropdownButtonFormField<String>), find.byType(SegmentedButton<String>), find.widgetWithText(OutlinedButton, 'Audio test'), find.widgetWithText(TextButton, 'Copy the report')]) {
          await reveal(tester, t);
          expect(tester.getSize(t).height, greaterThanOrEqualTo(48), reason: '$t');
        }
        expect(tester.takeException(), isNull);
      });
    }
  }

  testWidgets('avatar on or off is saved', (tester) async {
    await show(tester);
    expect(settings.avatarEnabled, isTrue);
    await press(tester, find.byKey(const ValueKey('avatar-enabled')));
    expect(settings.avatarEnabled, isFalse);
    final again = await AppSettings.load();
    expect(again.avatarEnabled, isFalse, reason: 'it is on the phone, not in memory');
  });

  testWidgets('the animation systems are listed with weight and a note; three can be chosen today and the others say they are not available', (tester) async {
    await show(tester);
    for (final e in avatarEngines) {
      expect(find.text(Texts.t(e.nameKey)), findsOneWidget);
      expect(find.text(Texts.t(e.weightKey)), findsOneWidget);
      expect(find.text(Texts.t(e.noteKey)), findsOneWidget);
    }
    expect(find.text('Not available in this build'), findsNWidgets(2));
    expect(find.text('Only the video from the PC is available in this build.'), findsOneWidget);
    final handle = tester.ensureSemantics();
    expect(tester.getSemantics(find.byKey(const ValueKey('engine-video'))), isSemantics(isSelected: true, isEnabled: true));
    expect(tester.getSemantics(find.byKey(const ValueKey('engine-ultralight'))), isSemantics(isEnabled: false));
    expect(tester.getSemantics(find.byKey(const ValueKey('engine-ultralight'))).label, contains('Not available in this build'));
    handle.dispose();

    await press(tester, find.byKey(const ValueKey('engine-ultralight')));
    await press(tester, find.byKey(const ValueKey('engine-feathertalk')));
    expect(settings.avatarMode, 'video', reason: 'a disabled engine cannot be chosen');

    await press(tester, find.byKey(const ValueKey('engine-wav2lip')));
    expect(settings.avatarMode, 'wav2lip');
    expect(find.textContaining('comes in a later phase'), findsOneWidget);
    await press(tester, find.byKey(const ValueKey('engine-visemes')));
    expect(settings.avatarMode, 'visemes');
    await press(tester, find.byKey(const ValueKey('engine-video')));
    expect(settings.avatarMode, 'video');
    expect(find.textContaining('comes in a later phase'), findsNothing);
  });

  testWidgets('the list is the data: every engine of avatarEngines has a row, and only the chosen engine is marked', (tester) async {
    await show(tester, prefs: {'flutter.avatarMode': 'visemes'});
    final handle = tester.ensureSemantics();
    for (final e in avatarEngines) {
      final node = tester.getSemantics(find.byKey(ValueKey('engine-${e.id}')));
      expect(node, e.id == 'visemes' ? isSemantics(isSelected: true) : isNot(isSemantics(isSelected: true)));
    }
    handle.dispose();
    expect(selectableEngines, ['visemes', 'wav2lip', 'video']);
  });

  testWidgets('resolution and quality are saved, and the quality says what it means', (tester) async {
    await show(tester);
    expect(settings.avatarResolution, 256);
    await reveal(tester, find.byType(DropdownButtonFormField<int>));
    await tester.tap(find.byType(DropdownButtonFormField<int>));
    await tester.pumpAndSettle();
    for (final r in avatarResolutions) {
      expect(find.text('$r px'), findsWidgets);
    }
    await tester.tap(find.text('128 px').last);
    await tester.pumpAndSettle();
    expect(settings.avatarResolution, 128);

    expect(find.textContaining('25 frames per second, about 800 kbit/s'), findsOneWidget);
    await tester.tap(find.byType(DropdownButtonFormField<AvatarQuality>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('High').last);
    await tester.pumpAndSettle();
    expect(settings.avatarQuality, AvatarQuality.high);
    expect(find.textContaining('30 frames per second, about 1800 kbit/s'), findsOneWidget);
    final again = await AppSettings.load();
    expect(again.avatarResolution, 128);
    expect(again.avatarQuality, AvatarQuality.high);
  });

  testWidgets('the personalities of the PC are offered and the choice is saved; Direct or Harness by default too', (tester) async {
    await show(tester);
    await reveal(tester, find.byType(DropdownButtonFormField<String>));
    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Bella').last);
    await tester.pumpAndSettle();
    expect(settings.avatarPersona, 'bella');

    await reveal(tester, find.byType(SegmentedButton<String>));
    await tester.tap(find.text('Harness'));
    await tester.pump();
    expect(settings.avatarBrain, 'harness');
    await tester.tap(find.text('Direct'));
    await tester.pump();
    expect(settings.avatarBrain, 'direct');
  });

  testWidgets('a personality that is saved but not in the list is still shown', (tester) async {
    await show(tester, prefs: {'flutter.avatarPersona': 'gone'});
    expect(find.text('gone'), findsOneWidget);
  });

  testWidgets('audio: the route is saved; Bluetooth is asked for when missing; full duplex on the speakerphone waits for the audio test', (tester) async {
    await show(tester);
    await reveal(tester, find.byKey(const ValueKey('setroute-earpiece')));
    await press(tester, find.byKey(const ValueKey('setroute-earpiece')));
    expect(settings.audioRoute, 'earpiece');
    router.permission = false;
    router.grantWhenAsked = false;
    await press(tester, find.byKey(const ValueKey('setroute-bluetooth')));
    expect(router.calls, contains('request'));
    expect(settings.audioRoute, 'earpiece');
    expect(find.textContaining('Without Android\'s permission for nearby devices'), findsOneWidget);
    router.grantWhenAsked = true;
    await press(tester, find.byKey(const ValueKey('setroute-bluetooth')));
    expect(settings.audioRoute, 'bluetooth');

    await reveal(tester, find.byKey(const ValueKey('full-duplex')));
    expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('full-duplex'))).onChanged, isNull);
    expect(find.textContaining('Run the audio test first'), findsOneWidget);
    expect(find.textContaining('Depends on the phone\'s echo canceller: run the audio test'), findsOneWidget);
  });

  testWidgets('the audio test shows its report as text to copy, and unlocks full duplex when the phone has an echo canceller', (tester) async {
    await show(tester);
    await reveal(tester, find.text('Audio test'));
    expect(find.byKey(const ValueKey('audio-report')), findsNothing);
    await press(tester, find.text('Audio test'));
    await reveal(tester, find.byKey(const ValueKey('audio-report')));
    final shown = tester.widget<SelectableText>(find.byKey(const ValueKey('audio-report'))).data!;
    expect(shown, contains('AcousticEchoCanceler available: yes'));
    expect(shown, contains('Echo canceller attaches to a VOICE_COMMUNICATION capture: yes'));
    expect(router.calls, contains('probe'));
    expect(settings.audioEchoCancel, isTrue);

    await reveal(tester, find.byKey(const ValueKey('full-duplex')));
    expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('full-duplex'))).onChanged, isNotNull);
    await press(tester, find.byKey(const ValueKey('full-duplex')));
    expect(settings.fullDuplexSpeaker, isTrue);
    expect(route.fullDuplex, isTrue);
  });

  testWidgets('a phone with no echo canceller keeps full duplex locked', (tester) async {
    await show(tester);
    router.report = const AudioReport(echoCancel: false, echoCancelOnCapture: 'no');
    await reveal(tester, find.text('Audio test'));
    await press(tester, find.text('Audio test'));
    await reveal(tester, find.byKey(const ValueKey('full-duplex')));
    expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('full-duplex'))).onChanged, isNull);
    expect(settings.audioEchoCancel, isFalse);
  });

  testWidgets('in Italian', (tester) async {
    await show(tester, lang: true);
    expect(find.text('Sistema di animazione'), findsOneWidget);
    expect(find.text('Risoluzione'), findsOneWidget);
    expect(find.text('Non disponibile in questa versione'), findsNWidgets(2));
  });

  group('what is saved reaches the PC through the Avatar tab', () {
    test('the personality, who answers and the avatar switch chosen in Settings are sent once to the connected gateway', () async {
      Texts.use('en');
      SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone'});
      final s = await AppSettings.load();
      final devices = FakeDevices();
      final connector = FakeConnector();
      final c = AvatarController(api: FakeGatewayApi(s), settings: s, devices: devices, client: GatewayClient(connector: connector.call, wait: (d) async {}));
      addTearDown(c.dispose);
      await c.activate();
      await pumpEventQueue();
      final ch = connector.channel;
      ch.say({...readyMessage, 'avatar': true});
      await pumpEventQueue();
      expect(ch.kinds, contains('video_start'));
      final start = ch.last('video_start')!;
      expect(start['resolution'], 256);
      expect(start['fps'], 25);
      expect(start['bitrate_kbps'], 800);

      await s.setAvatarPersona('otto');
      await s.setAvatarBrain('harness');
      await pumpEventQueue();
      expect(ch.last('persona'), {'type': 'persona', 'persona': 'otto'});
      expect(ch.last('brain'), {'type': 'brain', 'brain': 'harness'});
      final n = ch.sent.length;
      await s.setAvatarResolution(384);
      await pumpEventQueue();
      expect(ch.sent.length, n, reason: 'a change that is not the PC\'s business sends nothing');

      await s.setAvatarEnabled(false);
      await pumpEventQueue();
      expect(ch.last('video_stop'), isNotNull);
      ch.say({'type': 'video_stopped'});
      await pumpEventQueue();
      await s.setAvatarEnabled(true);
      await pumpEventQueue();
      expect(ch.kinds.where((k) => k == 'video_start').length, 2);
      expect(ch.last('video_start')!['resolution'], 384);
    });

    test('with the avatar off at the start, the PC is told to stop the video and none is asked for', () async {
      Texts.use('en');
      SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', 'flutter.avatarEnabled': false});
      final s = await AppSettings.load();
      final connector = FakeConnector();
      final c = AvatarController(api: FakeGatewayApi(s), settings: s, devices: FakeDevices(), client: GatewayClient(connector: connector.call, wait: (d) async {}));
      addTearDown(c.dispose);
      await c.activate();
      await pumpEventQueue();
      connector.channel.say({...readyMessage, 'avatar': true});
      await pumpEventQueue();
      expect(connector.channel.kinds, contains('video_stop'));
      expect(connector.channel.kinds, isNot(contains('video_start')));
    });
  });
}
