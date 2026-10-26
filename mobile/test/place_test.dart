// The position goes to the PC only when the person switched it on and a message has one.
import 'dart:convert';
import 'dart:io';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/place.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  test('the switch is off at first and nothing is read while it is off', () async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load();
    expect(settings.sendPlace, isFalse);
    expect(await PlaceFinder.of(settings).current(), isNull);
  });

  test('a message carries the position in its body only when it has one', () async {
    HttpOverrides.global = null;
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final bodies = <Map<String, dynamic>>[];
    server.listen((req) async {
      final text = await utf8.decodeStream(req);
      if (req.method == 'POST') bodies.add(Map<String, dynamic>.from(jsonDecode(text) as Map));
      req.response
        ..statusCode = 200
        ..headers.contentType = ContentType.json
        ..write('{}');
      await req.response.close();
    });
    SharedPreferences.setMockInitialValues({'flutter.address': '127.0.0.1:${server.port}', 'flutter.key': 'k'});
    final settings = await AppSettings.load();
    final ctl = ChatController(Api(settings), settings)..sid = 's1';
    await ctl.send('ciao');
    await ctl.send('ristoranti qui vicino', place: {'lat': 45.46427, 'lon': 9.18951, 'address': {'city': 'Milano'}});
    ctl.dispose();
    await server.close(force: true);
    expect(bodies[0].containsKey('place'), isFalse);
    expect((bodies[1]['place'] as Map)['address']['city'], 'Milano');
  });
}
