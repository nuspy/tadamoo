import 'dart:convert';

import 'package:basalt_mobile/src/actions.dart';
import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:basalt_mobile/src/tasks_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

Map<String, dynamic> register() => {
      'projects': [
        {
          'name': 'basalt',
          'path': 'E:/Projects/basalt',
          'repo_url': 'https://github.com/nuspy/tadamoo',
          'tasks': [
            {
              'id': 't-1', 'num': 7, 'depth': 0, 'title': 'Ship the release', 'state': 'running', 'description': 'Windows and Android', 'done': 'the engine', 'missing': 'the upload', 'plan': null,
              'links': {'session': '', 'plan': '', 'files': [], 'git_branch': 'feat/release', 'commits': ['abcdef1234567890', '0123456']},
            },
            {'id': 't-2', 'num': 0, 'depth': 1, 'title': 'Upload', 'state': 'waiting', 'description': '', 'done': '', 'missing': '', 'plan': null, 'links': {'session': '', 'plan': '', 'files': []}},
          ],
        },
        {'name': 'empty', 'path': 'E:/Projects/empty', 'repo_url': '', 'tasks': []},
      ],
    };

void main() {
  Future<Api> api() async {
    Texts.use('en');
    SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone'});
    return Api(await AppSettings.load(secrets: FakeSecretStore()));
  }

  http.Response reply(Object body) => http.Response.bytes(utf8.encode(jsonEncode(body)), 200, headers: {'content-type': 'application/json; charset=utf-8'});

  testWidgets('the tasks of the PC are listed read only, with their state, their branch and their commits', (tester) async {
    final a = await api();
    final asked = <String>[];
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(home: TasksScreen(api: a)));
      await tester.pumpAndSettle();
      expect(find.text('basalt'), findsOneWidget);
      expect(find.text('empty'), findsNothing);                                                        // a project with no open task is not listed
      expect(find.text('Ship the release'), findsOneWidget);
      expect(find.text('#7'), findsOneWidget);
      expect(find.text('Running'), findsOneWidget);
      expect(find.text('Waiting for you'), findsOneWidget);
      expect(find.text('Done so far: the engine'), findsOneWidget);
      expect(find.text('Still missing: the upload'), findsOneWidget);
      expect(find.text('feat/release'), findsOneWidget);
      expect(find.text('abcdef12'), findsOneWidget);
      expect(find.text('0123456'), findsOneWidget);
      expect(find.byType(ActionChip), findsNWidgets(3));                                               // the branch and two commits open the remote's page
      expect(find.text('Read only: tasks are changed on the PC.'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(find.byType(FilledButton), findsNothing);
    }, () => MockClient((r) async {
          asked.add('${r.method} ${r.url.path}?${r.url.query}');
          return reply(register());
        }));
    expect(asked, ['GET /api/register?project=all']);
  });

  testWidgets('without a remote the branch and the commits are plain text, and with nothing open the screen says so', (tester) async {
    final a = await api();
    final noRemote = register();
    ((noRemote['projects'] as List).first as Map)['repo_url'] = '';
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(home: TasksScreen(api: a)));
      await tester.pumpAndSettle();
      expect(find.byType(ActionChip), findsNothing);
      expect(find.byType(Chip), findsNWidgets(5));                                                      // two states, the branch and two commits
    }, () => MockClient((r) async => reply(noRemote)));
    await http.runWithClient(() async {
      await tester.pumpWidget(MaterialApp(home: TasksScreen(key: const ValueKey('nothing open'), api: a)));
      await tester.pumpAndSettle();
      expect(find.text('No open tasks.'), findsOneWidget);
    }, () => MockClient((r) async => reply({'projects': []})));
  });

  testWidgets('the tab offers the tasks as a third part when it is given them, and not otherwise', (tester) async {
    final a = await api();
    final feed = ActionsFeed(a);
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: ActionsScreen(feed: feed, jobs: const SizedBox(), tasks: const Text('TASKS PART')))));
    expect(find.text('Tasks'), findsOneWidget);
    await tester.tap(find.text('Tasks'));
    await tester.pumpAndSettle();
    expect(find.text('TASKS PART'), findsOneWidget);
    await tester.pumpWidget(MaterialApp(home: Scaffold(body: ActionsScreen(feed: feed, jobs: const SizedBox()))));
    await tester.pumpAndSettle();
    expect(find.text('Tasks'), findsNothing);
    feed.dispose();
  });
}
