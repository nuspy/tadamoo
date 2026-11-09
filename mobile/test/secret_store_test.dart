import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

void main() {
  late FakeSecretStore secrets;
  late List<String> logs;

  Future<AppSettings> load(Map<String, Object> prefs) async {
    SharedPreferences.setMockInitialValues(prefs);
    return AppSettings.load(secrets: secrets, log: logs.add);
  }

  setUp(() {
    secrets = FakeSecretStore();
    logs = [];
  });

  test('a key left in the preferences moves once to the secure store and is deleted from the preferences', () async {
    final s = await load({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_old'});
    expect(s.key, 'bsk_old');
    expect(s.keyIsSecure, isTrue);
    expect(secrets.data['key'], 'bsk_old');
    expect((await SharedPreferences.getInstance()).containsKey('key'), isFalse);
    expect(logs.single, contains('moved'));
    // the next start reads it from the secure store, nothing left to migrate
    logs.clear();
    final again = await AppSettings.load(secrets: secrets, log: logs.add);
    expect(again.key, 'bsk_old');
    expect(logs, isEmpty);
  });

  test('a new key is written to the secure store and never to the preferences', () async {
    final s = await load({});
    await s.setServer(address: ' 100.64.1.2:7232 ', key: ' bsk_new ');
    expect(s.key, 'bsk_new');
    expect(s.configured, isTrue);
    expect(secrets.data['key'], 'bsk_new');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.containsKey('key'), isFalse);
    expect(prefs.getKeys().any((k) => prefs.get(k).toString().contains('bsk_new')), isFalse);
  });

  test('an empty key removes it from the secure store', () async {
    final s = await load({});
    await s.setServer(address: 'a:1', key: 'bsk_x');
    await s.setServer(address: 'a:1', key: '');
    expect(s.key, '');
    expect(secrets.data.containsKey('key'), isFalse);
  });

  test('a stored key is read at start without touching the preferences', () async {
    secrets.data['key'] = 'bsk_kept';
    final s = await load({'flutter.address': 'a:1'});
    expect(s.key, 'bsk_kept');
    expect(s.configured, isTrue);
    expect(logs, isEmpty);
  });

  test('a key the preferences got while the store was down wins and replaces the stale secure one', () async {
    secrets.data['key'] = 'bsk_stale';
    final s = await load({'flutter.key': 'bsk_fresh'});
    expect(s.key, 'bsk_fresh');
    expect(secrets.data['key'], 'bsk_fresh');
  });

  test('without a secure store the app keeps working on the preferences, says so in the log and in keyIsSecure', () async {
    secrets.broken = true;
    final s = await load({'flutter.address': 'a:1', 'flutter.key': 'bsk_old'});
    expect(s.key, 'bsk_old');
    expect(s.configured, isTrue);
    expect(s.keyIsSecure, isFalse);
    expect(logs.single, contains('secure store unavailable'));
    expect(logs.single, isNot(contains('bsk_old')));
    await s.setServer(address: 'a:1', key: 'bsk_next');
    expect(s.key, 'bsk_next');
    expect(s.keyIsSecure, isFalse);
    expect((await SharedPreferences.getInstance()).getString('key'), 'bsk_next');
    expect(logs.length, 2);
  });

  test('a write that does not read back leaves the key in the preferences and reports it', () async {
    secrets.dropWrites = true;
    final s = await load({'flutter.key': 'bsk_old'});
    expect(s.key, 'bsk_old');
    expect(s.keyIsSecure, isFalse);
    expect((await SharedPreferences.getInstance()).getString('key'), 'bsk_old');
  });

  test('when the store comes back the plain key is moved into it', () async {
    secrets.broken = true;
    final s = await load({'flutter.key': 'bsk_old'});
    expect(s.keyIsSecure, isFalse);
    secrets.broken = false;
    final again = await AppSettings.load(secrets: secrets, log: logs.add);
    expect(again.keyIsSecure, isTrue);
    expect(secrets.data['key'], 'bsk_old');
    expect((await SharedPreferences.getInstance()).containsKey('key'), isFalse);
  });
}
