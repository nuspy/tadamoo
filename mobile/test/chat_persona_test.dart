import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/chat_screen.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/voice.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

class NamesServer extends Api {
  NamesServer(super.settings, {this.down = false});
  final bool down;
  final List<String> calls = [];

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    calls.add('GET $path');
    if (path == '/api/persona-names') {
      if (down) throw ApiError(404, 'not found');
      return {
        'personas': [
          {'slug': 'shan', 'name': 'Shan', 'kind': 'avatar'},
          {'slug': 'il-critico', 'name': 'Il Critico', 'kind': 'colleague'},
        ]
      };
    }
    throw ApiError(404, 'not found');
  }

  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path $body');
    if (path.endsWith('/persona')) {
      return {'session': {'id': 's1', 'persona': body!['persona'], 'state': 'idle'}};
    }
    throw ApiError(404, 'not found');
  }
}

void main() {
  setUp(() => Texts.use('en'));

  Future<NamesServer> open(WidgetTester t, {bool down = false}) async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final api = NamesServer(settings, down: down);
    await t.pumpWidget(MaterialApp(home: ChatScreen(api: api, settings: settings, voice: Voice(settings))));
    await t.pump();
    return api;
  }

  testWidgets('one small icon in the bar opens the avatars and the colleagues, and the choice is shown in its tip', (t) async {
    await open(t);
    final menu = find.byKey(const ValueKey('persona-menu'));
    expect(menu, findsOneWidget);
    expect(t.getSize(menu).width, lessThanOrEqualTo(48));                       // an icon, not a field
    expect(t.widget<PopupMenuButton<String>>(menu).tooltip, 'Who answers');
    await t.tap(menu);
    await t.pumpAndSettle();
    expect(find.text('Avatars'), findsOneWidget);
    expect(find.text('Colleagues'), findsOneWidget);
    expect(find.text('Shan'), findsOneWidget);
    await t.tap(find.text('Il Critico'));
    await t.pumpAndSettle();
    expect(t.widget<PopupMenuButton<String>>(menu).tooltip, 'Answering as Il Critico');   // a conversation not yet made keeps it for the request that makes it
    await t.tap(menu);
    await t.pumpAndSettle();
    await t.tap(find.text('None'));
    await t.pumpAndSettle();
    expect(t.widget<PopupMenuButton<String>>(menu).tooltip, 'Who answers');
  });

  testWidgets('an older PC without the list shows no menu', (t) async {
    await open(t, down: true);
    expect(find.byKey(const ValueKey('persona-menu')), findsNothing);
  });

  test('a conversation changes who answers with one call and a new one carries it in its request', () async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final api = NamesServer(settings);
    final ctl = ChatController(api, settings)..sid = 's1';
    await ctl.setPersona('il-critico');
    expect(api.calls.last, 'POST /api/agent/sessions/s1/persona {persona: il-critico}');
    expect(ctl.info!.persona, 'il-critico');
    expect(NewChat().copy(persona: 'shan').body()['persona'], 'shan');
    expect(NewChat().body().containsKey('persona'), isFalse);
  });
}
