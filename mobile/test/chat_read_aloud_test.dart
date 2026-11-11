import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_screen.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/voice.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final calls = <String>[];

  setUp(() {
    Texts.use('en');
    calls.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(const MethodChannel('flutter_tts'), (call) async {
      calls.add(call.method);
      return 1;
    });
  });

  Future<(AppSettings, Voice)> open(WidgetTester t) async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final voice = Voice(settings);
    await t.pumpWidget(MaterialApp(home: ChatScreen(api: Api(settings), settings: settings, voice: voice)));
    return (settings, voice);
  }

  Finder loudspeaker() => find.byKey(const ValueKey('read-aloud'));
  IconData shown(WidgetTester t) => (t.widget<IconButton>(loudspeaker()).icon as Icon).icon!;

  testWidgets('the loudspeaker is in the bar whether or not anything is being read, and turns reading aloud off and on again', (t) async {
    final (settings, voice) = await open(t);
    expect(settings.autoSpeak, isFalse);
    expect(loudspeaker(), findsOneWidget);
    expect(shown(t), Icons.volume_off);

    await t.tap(loudspeaker());
    await t.pump();
    expect(settings.autoSpeak, isTrue);
    expect(shown(t), Icons.volume_up);

    await t.tap(loudspeaker());
    await t.pump();
    expect(settings.autoSpeak, isFalse);
    expect(shown(t), Icons.volume_off);
    expect(calls, contains('stop'));          // turning it off also silences what is being read

    await t.tap(loudspeaker());               // and it can be turned on again: the icon did not go away
    await t.pump();
    expect(settings.autoSpeak, isTrue);
    expect(voice.speaking, isFalse);
  });

  testWidgets('the tooltip says what the tap does', (t) async {
    await open(t);
    expect(t.widget<IconButton>(loudspeaker()).tooltip, 'Turn reading aloud on');
    await t.tap(loudspeaker());
    await t.pump();
    expect(t.widget<IconButton>(loudspeaker()).tooltip, 'Turn reading aloud off');
  });

  testWidgets('what the phone voice says when it cannot read is shown once', (t) async {
    final (_, voice) = await open(t);
    voice.speechProblem = 'Error from TextToSpeech (speak) - -4';
    voice.notifyListeners();
    await t.pump();
    expect(find.text('Error from TextToSpeech (speak) - -4'), findsOneWidget);
    expect(voice.speechProblem, isNull);
  });
}
