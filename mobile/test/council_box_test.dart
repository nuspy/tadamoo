import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/chat_options.dart';
import 'package:basalt_mobile/src/chat_screen.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/server_prefs.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/voice.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

class Quiet extends Api {
  Quiet(super.settings);
  final List<String> calls = [];

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    calls.add('GET $path');
    throw ApiError(404, 'not found');
  }

  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path $body');
    throw ApiError(404, 'not found');
  }
}

void main() {
  setUp(() => Texts.use('en'));

  test('a conversation starts with the council box only when it was on, and the PC page sends the same field', () {
    expect(NewChat().body().containsKey('council'), isFalse);
    expect(NewChat().copy(council: true).body()['council'], isTrue);
    expect(NewChat(persona: 'shan', council: true).copy(role: 'build').council, isTrue);
  });

  test('the box is read from the conversation and changed with the PC route', () {
    expect(SessionInfo({'id': 's1'}).council, isFalse);
    expect(SessionInfo({'id': 's1', 'council': true}).council, isTrue);
  });

  testWidgets('the checkbox in the options is small, sends the change and says why when the PC refuses', (t) async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final prefs = ServerPrefs(Quiet(settings));
    final asked = <bool>[];
    var refuse = false;
    await t.pumpWidget(MaterialApp(
      home: Builder(
        builder: (c) => TextButton(
          onPressed: () => showChatOptions(c, prefs: prefs, onCouncil: (on) async {
            asked.add(on);
            return refuse ? 'the council needs two advisers' : null;
          }),
          child: const Text('open'),
        ),
      ),
    ));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    final box = find.byKey(const ValueKey('council-box'));
    expect(box, findsOneWidget);
    expect(find.text('Use the Council'), findsOneWidget);
    expect(t.getSize(box).height, greaterThanOrEqualTo(48)); // a touch target, a row and not a form
    expect(t.widget<CheckboxListTile>(box).value, isFalse);
    await t.tap(box);
    await t.pumpAndSettle();
    expect(asked, [true]);
    expect(t.widget<CheckboxListTile>(box).value, isTrue);
    refuse = true;
    await t.tap(box);
    await t.pumpAndSettle();
    expect(asked, [true, false]);
    expect(t.widget<CheckboxListTile>(box).value, isTrue); // refused: it stays as it was
    expect(find.text('the council needs two advisers'), findsOneWidget);
  });

  testWidgets('in a conversation not yet made the box is kept for the request that makes it, and a small icon in the bar says it is on', (t) async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final api = Quiet(settings);
    await t.pumpWidget(MaterialApp(home: ChatScreen(api: api, settings: settings, voice: Voice(settings))));
    await t.pump();
    expect(find.byKey(const ValueKey('council-on')), findsNothing);
    await t.tap(find.byTooltip('Conversation options'));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const ValueKey('council-box')));
    await t.pumpAndSettle();
    expect(api.calls.where((c) => c.contains('/council')), isEmpty); // no conversation yet: nothing is sent
    await t.tapAt(const Offset(5, 5)); // closes the sheet
    await t.pumpAndSettle();
    final icon = find.byKey(const ValueKey('council-on'));
    expect(icon, findsOneWidget);
    expect(t.getSize(icon).width, lessThanOrEqualTo(48));
    expect(t.widget<IconButton>(icon).tooltip, 'Council is on in this conversation');
  });

  test('the events of the council are told in the conversation', () {
    expect(Texts.t('council.now.on'), isNot('council.now.on'));
    Texts.use('it');
    expect(Texts.t('council.title'), 'Usa il Consiglio');
  });
}
