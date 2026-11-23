import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

/// Records what the app sends to the server.
class Recorder extends Api {
  Recorder(super.settings);
  final List<String> calls = [];

  @override
  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body]) async {
    calls.add('POST $path $body');
    return {};
  }
}

Future<(ChatController, Recorder)> open() async {
  SharedPreferences.setMockInitialValues({});
  final settings = await AppSettings.load(secrets: FakeSecretStore());
  final api = Recorder(settings);
  final c = ChatController(api, settings)..sid = 's1';
  return (c, api);
}

List<ChatItem> notes(ChatController c) => c.items.where((x) => x.kind == 'note').toList();

void main() {
  setUp(() => Texts.use('en'));

  group('a message written while the agent works', () {
    test('what the harness decided is told in a note, with the place in the queue', () async {
      final (c, _) = await open();
      c.applyEvents([
        {'event': 'queued', 'turn': 't2'},
        {'event': 'routed', 'turn': 't2', 'how': 'queue', 'position': 2},
        {'event': 'routed', 'turn': 't3', 'how': 'steer'},
        {'event': 'routed', 'turn': 't4', 'how': 'urgent'},
        {'event': 'routed', 'turn': 't5', 'how': 'parallel'},
      ]);
      final n = notes(c);
      expect(n, hasLength(4), reason: 'queued adds nothing: the person\'s own bubble is already there');
      expect(n[0].text, 'It can wait: it starts when the agent is done (2 waiting).');
      expect(n[0].how, 'queue');
      expect(n[1].text, 'Added to what the agent is doing.');
      expect(n[2].text, startsWith('Urgent:'));
      expect(n[3].text, startsWith('The model can do it beside the task'));
    });

    test('only a waiting request keeps its buttons, and loses them when it starts', () async {
      final (c, _) = await open();
      c.applyEvents([
        {'event': 'routed', 'turn': 't2', 'how': 'queue', 'position': 1},
        {'event': 'routed', 'turn': 't3', 'how': 'queue', 'position': 2},
      ]);
      expect(notes(c).map((x) => x.over), [false, false]);
      c.applyEvents([
        {'event': 'started', 'turn': 't2'},
      ]);
      expect(notes(c).map((x) => x.over), [true, false], reason: 'the first request began; the second still waits');
    });

    test('a request taken out of the queue says so and loses its buttons', () async {
      final (c, _) = await open();
      c.applyEvents([
        {'event': 'routed', 'turn': 't2', 'how': 'queue', 'position': 1},
        {'event': 'dropped', 'turn': 't2'},
      ]);
      final n = notes(c);
      expect(n.first.over, isTrue);
      expect(n.last.text, 'Removed from the queue.');
    });

    test('"Do it now" and "Remove" tell the server which request they mean', () async {
      final (c, api) = await open();
      c.applyEvents([
        {'event': 'routed', 'turn': 't2', 'how': 'queue', 'position': 1},
      ]);
      final note = notes(c).single;
      await c.promote(note);
      await c.drop(note);
      expect(api.calls, [
        'POST /api/agent/sessions/s1/promote {turn: t2}',
        'POST /api/agent/sessions/s1/drop {turn: t2}',
      ]);
    });

    test('a note without a turn cannot be promoted or dropped', () async {
      final (c, api) = await open();
      await c.promote(ChatItem('note', text: 'x'));
      await c.drop(ChatItem('note', text: 'x'));
      expect(api.calls, isEmpty);
    });

    test('sending while busy goes to the same route as sending while idle', () async {
      final (c, api) = await open();
      c.state = 'busy';
      await c.send('and also check the tests');
      expect(api.calls.single, startsWith('POST /api/agent/sessions/s1/send {text: and also check the tests'));
    });
  });
}
