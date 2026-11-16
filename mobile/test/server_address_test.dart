import 'dart:convert';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/server_key.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

void main() {
  test('the address is http or https with a name or an IP address, a port or none', () {
    String? p(String a) => AppSettings.parseServerAddress(a)?.toString();
    expect(p('100.64.1.2:7232'), 'http://100.64.1.2:7232');
    expect(p('http://hrns.tadamoo.com'), 'http://hrns.tadamoo.com');
    expect(p('https://hrns.tadamoo.com'), 'https://hrns.tadamoo.com');
    expect(p('https://hrns.tadamoo.com:8443/'), 'https://hrns.tadamoo.com:8443');
    expect(p('mio-server.local:7232'), 'http://mio-server.local:7232');
    for (final bad in ['', 'ftp://x', 'http://x/path', 'http://x?a=1', 'http://u:p@x']) {
      expect(p(bad), isNull, reason: bad);
    }
  });

  test('requests go to the https name with the key of the entry, and the key change posts the current and the new key', () async {
    SharedPreferences.setMockInitialValues({'flutter.address': 'https://hrns.tadamoo.com', 'flutter.key': 'test-fake-key-1'});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    final seen = <http.Request>[];
    await http.runWithClient(() => Api(settings).changeServerKey('test-fake-key-1', 'test-fake-key-2'), () => MockClient((r) async {
          seen.add(r);
          return http.Response(jsonEncode({'ok': true}), 200, headers: {'content-type': 'application/json'});
        }));
    expect(seen.single.url.toString(), 'https://hrns.tadamoo.com/api/server-key');
    expect(seen.single.headers['Authorization'], 'Bearer test-fake-key-1');
    expect(jsonDecode(seen.single.body), {'current': 'test-fake-key-1', 'new': 'test-fake-key-2'});
    await settings.setServer(address: settings.address, key: 'test-fake-key-2');
    expect(settings.key, 'test-fake-key-2');
  });

  testWidgets('the key dialog asks for the new key twice and gives back the current and the new one', (tester) async {
    Texts.use('en');
    (String, String)? answer;
    await tester.pumpWidget(MaterialApp(
      home: Builder(builder: (c) => TextButton(onPressed: () async => answer = await showDialog<(String, String)>(context: c, builder: (_) => const KeyChangeDialog(current: 'test-fake-key-1')), child: const Text('open'))),
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const ValueKey('sk-new')), 'test-fake-key-2');
    await tester.enterText(find.byKey(const ValueKey('sk-again')), 'other');
    await tester.tap(find.text('Change'));
    await tester.pumpAndSettle();
    expect(find.text('The two new keys are not the same.'), findsOneWidget);
    expect(answer, isNull);
    await tester.enterText(find.byKey(const ValueKey('sk-again')), 'test-fake-key-2');
    await tester.tap(find.text('Change'));
    await tester.pumpAndSettle();
    expect(answer, ('test-fake-key-1', 'test-fake-key-2'));
  });
}
