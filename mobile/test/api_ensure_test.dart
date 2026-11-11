import 'dart:convert';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

void main() {
  Future<AvatarEnsure> ask(int status, Object body, {String lang = 'en', List<http.Request>? seen}) async {
    Texts.use(lang);
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone'});
    final api = Api(await AppSettings.load(secrets: FakeSecretStore()));
    return http.runWithClient(() => api.ensureAvatar(), () => MockClient((r) async {
          seen?.add(r);
          return http.Response(jsonEncode(body), status, headers: {'content-type': 'application/json'});
        }));
  }

  test('it posts to the one route with the phone\'s key and says yes when the PC loaded the avatar', () async {
    final seen = <http.Request>[];
    final r = await ask(200, {'ok': true, 'state': 'loaded'}, seen: seen);
    expect(r.ok, isTrue);
    expect(seen.single.method, 'POST');
    expect(seen.single.url.toString(), 'http://100.64.1.2:7232/api/voice/avatar/ensure');
    expect(seen.single.headers['Authorization'], 'Bearer bsk_phone');
    expect(jsonDecode(seen.single.body), {'wait_s': 60});
  });

  test('a refusal carries its reason and its text in the language of the app', () async {
    const body = {'ok': false, 'reason': 'busy', 'message': 'Another job holds the VRAM.', 'message_it': 'Un altro lavoro tiene la VRAM.'};
    final en = await ask(409, body);
    expect((en.ok, en.reason, en.message), (false, 'busy', 'Another job holds the VRAM.'));
    final it = await ask(409, body, lang: 'it');
    expect(it.message, 'Un altro lavoro tiene la VRAM.');
  });

  test('a PC without the route is a yes, and an answer without text still says something', () async {
    expect((await ask(404, {'error': {'message': 'not found'}})).ok, isTrue);
    final r = await ask(500, <String, Object>{});
    expect((r.ok, r.message), (false, 'Error 500'));
  });
}
