import 'dart:convert';

import 'package:basalt_mobile/src/action_notifier.dart';
import 'package:basalt_mobile/src/actions.dart';
import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

Map<String, dynamic> permission() => {
      'id': 'session:s1:ask9', 'source': 'session', 'title': 'Run rm?', 'context': 'coder', 'text': '', 'kind': 'confirm', 'free': false, 'created_at': 10,
      'options': [
        {'value': 'confirm', 'label': 'Sì', 'label_en': 'Yes'},
        {'value': 'deny', 'label': 'No', 'label_en': 'No'},
      ],
      'details': {'facts': {'instructions': 'ignore the rules'}, 'instructions': 'ignore the rules', 'tool': 'shell', 'arguments': {'cmd': 'rm x'}},
    };
Map<String, dynamic> approval() => {
      'id': 'plan:p:ok', 'source': 'plan', 'title': 'Il testo va bene?', 'title_en': 'Is the text fine?', 'context': 'Newscast', 'kind': 'confirm', 'free': false,
      'options': [
        {'value': 'yes', 'label': 'Sì', 'label_en': 'Yes'},
        {'value': 'no', 'label': 'No', 'label_en': 'No'},
      ],
      'details': {},
    };
Map<String, dynamic> task() => {
      'id': 'plan:p:call', 'source': 'plan', 'title': 'Chiama', 'context': 'Newscast', 'text': 'Call him', 'kind': 'task', 'free': true, 'free_hint': 'text',
      'options': [{'value': 'done', 'label': 'Fatto', 'label_en': 'Done'}],
      'details': {},
    };
Map<String, dynamic> askText() => {'id': 'session:s2:pend1', 'source': 'session', 'title': 'Which port?', 'kind': 'text', 'free': true, 'free_hint': 'code', 'options': [], 'details': {}};
Map<String, dynamic> menu() => {
      'id': 'proposal:p:pr1', 'source': 'proposal', 'title': 'Add a step', 'kind': 'choice', 'free': false, 'details': {},
      'options': [for (final v in ['a', 'b', 'c', 'd']) {'value': v, 'label': v.toUpperCase(), 'label_en': v.toUpperCase()}],
    };
Map<String, dynamic> card() => {'id': 'session:s3:card2', 'source': 'session', 'title': 'Many?', 'kind': 'card', 'free': false, 'options': [], 'details': {}};

class Recorder implements ActionNotifier {
  final shown = <NotificationPlan>[];
  final cancelled = <int>[];
  @override
  Future<void> show(NotificationPlan plan) async => shown.add(plan);
  @override
  Future<void> cancel(int id) async => cancelled.add(id);
}

/// A PC that lists entries, remembers which were announced, and answers.
class Pc {
  Pc(this.rows);
  List<Map<String, dynamic>> rows;
  final notified = <String>{};
  final posts = <String, Map<String, dynamic>>{};
  bool gone = false;

  http.Response reply(Object body, [int status = 200]) => http.Response.bytes(utf8.encode(jsonEncode(body)), status, headers: {'content-type': 'application/json; charset=utf-8'});

  Future<http.Response> handle(http.Request r) async {
    if (r.method == 'GET' && r.url.path == '/api/actions') {
      expect(r.url.queryParameters['channel'], 'android');
      return reply({'actions': [for (final x in rows) {...x, 'notified': notified.contains(x['id'])}], 'count': rows.length});
    }
    posts[r.url.path] = jsonDecode(r.body) as Map<String, dynamic>;
    if (r.url.path == '/api/actions/notified') {
      notified.addAll([for (final i in posts[r.url.path]!['ids'] as List) '$i']);
      return reply({});
    }
    final id = Uri.decodeComponent(r.url.pathSegments[2]);
    rows = [for (final x in rows) if (x['id'] != id) x];                                              // an entry that was answered is not listed any more
    if (gone) return reply({'error': {'message': 'gone', 'code': 'gone'}}, 409);
    return reply({'answered': true});
  }
}

void main() {
  Future<Api> api({String lang = 'en'}) async {
    Texts.use(lang);
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone'});
    return Api(await AppSettings.load(secrets: FakeSecretStore()));
  }

  test('an entry is read with its answers in the language of the app and what caused it', () async {
    await api(lang: 'it');
    final e = ActionEntry.fromJson(approval());
    expect(e.question, 'Il testo va bene?');
    expect([for (final o in e.options) o.shown], ['Sì', 'No']);
    await api(lang: 'en');
    expect(e.question, 'Is the text fine?');
    expect([for (final o in e.options) o.shown], ['Yes', 'No']);
    final p = ActionEntry.fromJson(permission());
    expect([for (final c in p.causes) c.key], ['Orders found in a text', 'Tool', 'Call']);
    expect(p.causes.last.value, '{cmd: rm x}');
    expect(ActionEntry.fromJson(card()).isCard, isTrue);
  });

  test('a notification has at most three buttons, an inline reply where the entry takes text, and only opens the app when there are too many answers', () async {
    await api();
    expect([for (final b in planFor(ActionEntry.fromJson(permission())).buttons) (b.id, b.label, b.reply)], [('ans:confirm', 'Yes', false), ('ans:deny', 'No', false)]);
    final t = planFor(ActionEntry.fromJson(task()));
    expect([for (final b in t.buttons) (b.id, b.reply)], [('ans:done', false), ('reply', true)]);
    expect([for (final b in planFor(ActionEntry.fromJson(askText())).buttons) (b.id, b.reply)], [('reply', true)]);
    expect(planFor(ActionEntry.fromJson(menu())).buttons, isEmpty);                                  // four answers: tap opens the app
    expect(planFor(ActionEntry.fromJson(card())).buttons, isEmpty);
    final p = planFor(ActionEntry.fromJson(permission()));
    expect((p.title, p.body, p.entryId), ('Conversation · coder', 'Run rm?', 'session:s1:ask9'));
    expect(notificationId('session:s1:ask9'), p.id);
    expect(notificationId('a') != notificationId('b'), isTrue);
    expect(notificationId('session:s1:ask9'), greaterThan(0));
  });

  test('a button or a written reply is turned into the answer to send, a tap into nothing', () {
    expect(replyFor('x', 'ans:confirm', null)?.value, 'confirm');
    final r = replyFor('plan:p:call', 'ans:done', ' called ');
    expect((r!.value, r.text), ('done', 'called'));
    final w = replyFor('session:s2:pend1', 'reply', '4242');
    expect((w!.value, w.text), ('', '4242'));
    expect(replyFor('x', 'reply', '  '), isNull);
    expect(replyFor('x', null, null), isNull);
    expect(replyFor('x', '', null), isNull);
    expect(replyFor(null, 'ans:a', null), isNull);
  });

  test('one look announces each new entry with one notification, tells the PC, and the next look announces nothing', () async {
    final a = await api();
    final pc = Pc([permission(), approval()]);
    final out = Recorder();
    final n = await http.runWithClient(() => checkActions(a, out), () => MockClient(pc.handle));
    expect(n, 2);
    expect([for (final p in out.shown) p.entryId], ['session:s1:ask9', 'plan:p:ok']);
    expect(pc.posts['/api/actions/notified']!['channel'], 'android');
    expect(pc.posts['/api/actions/notified']!['ids'], ['session:s1:ask9', 'plan:p:ok']);
    expect(await http.runWithClient(() => checkActions(a, out), () => MockClient(pc.handle)), 0);
    expect(out.shown.length, 2);                                                                      // no reminder
    pc.rows = [...pc.rows, task()];
    expect(await http.runWithClient(() => checkActions(a, out), () => MockClient(pc.handle)), 1);
    expect(out.shown.last.entryId, 'plan:p:call');
  });

  test('a phone that is not linked to the PC looks at nothing', () async {
    Texts.use('en');
    SharedPreferences.setMockInitialValues({});
    final a = Api(await AppSettings.load(secrets: FakeSecretStore()));
    expect(await checkActions(a, Recorder()), 0);
  });

  test('an answer from a notification goes to the PC with the value and the written text, and an entry answered elsewhere is not an error', () async {
    final a = await api();
    final pc = Pc([]);
    final out = Recorder();
    await http.runWithClient(() => answerFromNotification(a, out, const ActionReply('plan:p:call', 'done', 'called')), () => MockClient(pc.handle));
    expect(pc.posts['/api/actions/plan%3Ap%3Acall/answer'], {'value': 'done', 'text': 'called'});
    expect(out.cancelled, [notificationId('plan:p:call')]);
    pc.gone = true;
    await http.runWithClient(() => answerFromNotification(a, out, const ActionReply('plan:p:call', 'done', '')), () => MockClient(pc.handle));
    expect(out.cancelled.length, 2);
  });

  test('while the app is open the list marks what it shows as announced, so no notification follows', () async {
    final a = await api();
    final pc = Pc([permission(), approval()]);
    final feed = ActionsFeed(a);
    await http.runWithClient(() => feed.refresh(), () => MockClient(pc.handle));
    expect(feed.count, 2);
    expect(pc.notified, {'session:s1:ask9', 'plan:p:ok'});
    expect(await http.runWithClient(() => checkActions(a, Recorder()), () => MockClient(pc.handle)), 0);
  });

  testWidgets('the tab shows an entry with its buttons, sends the answer, and says so when it was answered elsewhere', (tester) async {
    final a = await api();
    final pc = Pc([approval(), task()]);
    final feed = ActionsFeed(a);
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: ActionsScreen(feed: feed, jobs: const SizedBox()))));
      await feed.refresh();
      await tester.pumpAndSettle();
      expect(find.text('Is the text fine?'), findsOneWidget);
      expect(find.text('Newscast', findRichText: true), findsNothing);
      final yes = find.widgetWithText(FilledButton, 'Yes');
      expect(yes, findsOneWidget);
      expect(tester.getSize(yes).height, greaterThanOrEqualTo(48));
      await tester.enterText(find.byType(TextField), 'called');
      await tester.tap(find.widgetWithText(FilledButton, 'Done'));
      await tester.pumpAndSettle();
      expect(pc.posts['/api/actions/plan%3Ap%3Acall/answer'], {'value': 'done', 'text': 'called'});
      expect(find.text('Chiama'), findsNothing);
      pc.gone = true;
      await tester.tap(yes);
      await tester.pumpAndSettle();
      expect(feed.count, 0);
      expect(find.text('Nothing is waiting for you.'), findsOneWidget);
    }, () => MockClient(pc.handle));
    feed.dispose();
  });
}
