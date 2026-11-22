import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_screen.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/voice.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

/// A server with one conversation whose agent is working.
class BusyServer extends Api {
  BusyServer(super.settings);
  final List<String> calls = [];
  final List<Map<String, dynamic>> sent = [];

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    if (path == '/api/agent/sessions/s1/history') {
      return {
        'session': {'id': 's1', 'title': 'Work', 'state': 'busy', 'mode': 'default', 'images': 'high', 'charts': ''},
        'messages': <Object>[],
      };
    }
    if (path == '/api/agent/sessions/s1/events') return {'events': <Object>[], 'next': 0, 'state': 'busy'};
    throw ApiError(404, 'not found');
  }

  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path');
    if (path.endsWith('/send')) sent.add(body!);
    return {};
  }
}

void main() {
  setUp(() => Texts.use('en'));

  Future<(BusyServer, AppSettings)> open(WidgetTester t) async {
    t.view.physicalSize = const Size(360 * 3, 800 * 3);
    t.view.devicePixelRatio = 3;
    addTearDown(t.view.reset);
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final api = BusyServer(settings);
    await t.pumpWidget(MaterialApp(home: ChatScreen(api: api, settings: settings, voice: Voice(settings), sessionId: 's1')));
    await t.pump(const Duration(milliseconds: 50));
    return (api, settings);
  }

  Future<void> close(WidgetTester t) async {
    await t.pumpWidget(const SizedBox()); // stops the conversation's polling
    await t.pump(const Duration(seconds: 1));
  }

  testWidgets('while the agent works the person can both stop it and write to it', (t) async {
    final (api, _) = await open(t);
    expect(find.byTooltip('Stop'), findsOneWidget);
    expect(find.byTooltip('Send'), findsOneWidget);
    expect(find.text('Write: the agent reads it while it works'), findsOneWidget);
    await t.enterText(find.byType(TextField).last, 'and check the tests too');
    await t.tap(find.byTooltip('Send'));
    await t.pump(const Duration(milliseconds: 50));
    expect(api.sent.single['text'], 'and check the tests too');
    expect(api.calls, contains('POST /api/agent/sessions/s1/send'));
    expect(api.calls.any((c) => c.endsWith('/cancel')), isFalse, reason: 'writing is not stopping');
    await close(t);
  });

  testWidgets('the buttons of the bar are at least 48 wide and the box still fits a narrow phone', (t) async {
    await open(t);
    expect(t.takeException(), isNull);
    for (final tip in const ['+', 'mic', 'Stop', 'Send']) {
      expect(t.getSize(find.byTooltip(tip)).width, greaterThanOrEqualTo(48));
    }
    await close(t);
  });

  testWidgets('with the Long answer armed for the next message, that message ends with the line the harness reads, and only that one', (t) async {
    final (api, _) = await open(t);
    await t.tap(find.byTooltip('+'));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const ValueKey('long-answer')));
    await t.pumpAndSettle();
    await t.ensureVisible(find.text('Only this next prompt'));
    await t.tap(find.text('Only this next prompt'));
    await t.pumpAndSettle();
    expect(find.byKey(const ValueKey('long-chip')), findsOneWidget, reason: 'a chip says the Long answer is armed');
    await t.enterText(find.byType(TextField).last, 'write about bees');
    await t.tap(find.byTooltip('Send'));
    await t.pump(const Duration(milliseconds: 50));
    expect(api.sent.last['text'], 'write about bees\n\n[long answer: mode=write; size=fixed; words=3000]');
    await t.enterText(find.byType(TextField).last, 'thanks');
    await t.tap(find.byTooltip('Send'));
    await t.pump(const Duration(milliseconds: 50));
    expect(api.sent.last['text'], 'thanks', reason: '"only this next prompt" turned itself off');
    await close(t);
  });
}
