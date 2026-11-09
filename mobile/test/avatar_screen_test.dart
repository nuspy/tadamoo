import 'package:basalt_mobile/src/avatar_controller.dart';
import 'package:basalt_mobile/src/avatar_screen.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'avatar_fakes.dart';

void main() {
  late FakeDevices devices;
  late FakeConnector connector;
  late AvatarController c;

  Future<FakeChannel> show(WidgetTester tester, {Size size = const Size(360, 640), double text = 1.0, Map<String, Object> prefs = const {}, Map<String, dynamic>? ready}) async {
    Texts.use('en');
    tester.view.physicalSize = Size(size.width * 3, size.height * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone', ...prefs});
    final settings = await AppSettings.load();
    devices = FakeDevices();
    connector = FakeConnector();
    c = AvatarController(
      api: FakeGatewayApi(settings),
      settings: settings,
      devices: devices,
      client: GatewayClient(connector: connector.call, wait: (d) async {}),
    );
    addTearDown(c.dispose);
    await tester.pumpWidget(MaterialApp(
      theme: ThemeData.dark(),
      home: MediaQuery(
        data: MediaQueryData(size: size, textScaler: TextScaler.linear(text)),
        child: Scaffold(body: AvatarScreen(controller: c, active: true)),
      ),
    ));
    await tester.pump();
    await tester.pump();
    connector.channel.say(ready ?? readyMessage);
    await tester.pump();
    await tester.pump();
    return connector.channel;
  }

  for (final size in const [Size(360, 640), Size(390, 844), Size(1280, 800)]) {
    for (final text in const [1.0, 2.0]) {
      testWidgets('voice only, ${size.width.toInt()}x${size.height.toInt()} at text ${text}x: everything is there and nothing overflows', (tester) async {
        await show(tester, size: size, text: text);
        expect(tester.takeException(), isNull);
        expect(find.text('Connected to the PC.'), findsOneWidget);
        expect(find.text('Video from the PC'), findsOneWidget);
        expect(find.text('Wav2Lip on the phone'), findsOneWidget);
        expect(find.text('Visemes on a photo'), findsOneWidget);
        expect(find.textContaining('No avatar is loaded on the PC: voice only'), findsOneWidget);
        expect(find.text('Hold to talk'), findsOneWidget);
        expect(find.text('Direct'), findsOneWidget);
        expect(find.text('Harness'), findsOneWidget);
        final button = tester.getSize(find.byType(AnimatedContainer).last);
        expect(button.height, greaterThanOrEqualTo(48));
        await tester.drag(find.byType(ListView).first, const Offset(0, -400));
        await tester.pump();
        expect(tester.takeException(), isNull);
      });
    }
  }

  testWidgets('the choice of Wav2Lip is saved and the screen says it is not available in this build', (tester) async {
    final ch = await show(tester);
    await tester.tap(find.text('Wav2Lip on the phone'));
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('Not available in this build'), findsOneWidget);
    expect(find.textContaining('No avatar is loaded'), findsNothing);
    expect(c.settings.avatarMode, 'wav2lip');
    expect(ch.kinds, isNot(contains('video_start')));
    expect(find.text('Hold to talk'), findsOneWidget);                // the voice still works
  });

  testWidgets('who answers is chosen with the selector and goes to the PC', (tester) async {
    final ch = await show(tester);
    await tester.tap(find.text('Harness'));
    await tester.pump();
    await tester.pump();
    expect(ch.last('brain'), {'type': 'brain', 'brain': 'harness'});
  });

  testWidgets('the personalities of the PC are offered and the choice goes to the PC', (tester) async {
    final ch = await show(tester);
    await tester.scrollUntilVisible(find.text('The PC\'s own'), 200, scrollable: find.byType(Scrollable).first);
    await tester.tap(find.text('The PC\'s own'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('Bella').last);
    await tester.pump();
    await tester.pump();
    expect(ch.last('persona'), {'type': 'persona', 'persona': 'bella'});
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

  testWidgets('holding the button talks and releasing sends; a typed sentence is sent too', (tester) async {
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
    ch.say({'type': 'done'});
    await tester.pump();
    await tester.scrollUntilVisible(find.byType(TextField), 200, scrollable: find.byType(Scrollable).first);
    await tester.enterText(find.byType(TextField), 'what is the weather');
    await tester.tap(find.byIcon(Icons.send));
    await tester.pump();
    await tester.pump();
    expect(ch.last('text')!['text'], 'what is the weather');
    expect(find.text('what is the weather'), findsWidgets);
  });

  testWidgets('voice not loaded on the PC: the screen says what is missing and the button does nothing', (tester) async {
    final ch = await show(tester, ready: {...readyMessage, 'voice': false, 'missing': ['stt', 'tts']});
    expect(find.textContaining('speech recognition, speech synthesis'), findsOneWidget);
    await tester.tap(find.text('Hold to talk'));
    await tester.pump();
    expect(ch.kinds, isNot(contains('start')));
  });

  testWidgets('in Italian', (tester) async {
    await show(tester);
    Texts.use('it');
    await tester.pump();
    c.client.clearProblem();
    await tester.pump();
    expect(find.text('Tieni premuto per parlare'), findsOneWidget);
    expect(find.text('Video dal PC'), findsOneWidget);
  });
}
