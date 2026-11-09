// The app's side of the server's protocol, against a real server with a scripted model.
// Start it with `python mobile/tool/mock_server.py`, then:
//   flutter test --dart-define=BASALT_TEST_URL=http://127.0.0.1:<port> --dart-define=BASALT_TEST_FOLDER=<folder>
import 'dart:async';
import 'dart:io';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/chat_model.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

const url = String.fromEnvironment('BASALT_TEST_URL');
const folder = String.fromEnvironment('BASALT_TEST_FOLDER');

Future<void> until(bool Function() ok, {int seconds = 40}) async {
  final end = DateTime.now().add(Duration(seconds: seconds));
  while (!ok()) {
    if (DateTime.now().isAfter(end)) fail('timed out');
    await Future<void>.delayed(const Duration(milliseconds: 150));
  }
}

void main() {
  final skip = url.isEmpty ? 'BASALT_TEST_URL is not set' : null;
  late AppSettings settings;
  late Api api;

  setUp(() async {
    HttpOverrides.global = null;
    SharedPreferences.setMockInitialValues({'flutter.address': url.replaceFirst('http://', ''), 'flutter.key': 'bsk_not_needed_on_this_pc'});
    settings = await AppSettings.load(secrets: FakeSecretStore());
    api = Api(settings);
  });

  test('the address and key are read as the server wants them', () {
    expect(settings.configured, isTrue);
    expect(settings.base!.toString(), startsWith('http://127.0.0.1:'));
  }, skip: skip);

  test('hello answers', () async {
    final h = await api.hello();
    expect(h['ok'], isTrue);
  }, skip: skip);

  test('a conversation: create in a folder, send, follow the events to the answer, copy its text', () async {
    final done = Completer<ChatItem>();
    final ctl = ChatController(api, settings)..onAnswerDone = (a) => done.complete(a);
    await api.post('/api/agent/projects', {'path': folder});
    await ctl.create(workspace: folder, options: NewChat(mode: 'read_only'));
    expect(ctl.sid, isNotEmpty);
    await ctl.send('disegna il flusso');
    final answer = await done.future.timeout(const Duration(seconds: 60));
    expect(answer.answerText, contains('Ecco il flusso'));
    expect(answer.answerText, contains('basalt-path:report.txt'));
    expect(ctl.items.first.kind, 'user');
    await until(() => !ctl.busy);
    ctl.dispose();
    // it comes back from the saved history as it was
    final again = ChatController(api, settings);
    await again.open(ctl.sid);
    expect(again.items.where((i) => i.kind == 'assistant').first.answerText, contains('mermaid'));
    again.dispose();
  }, skip: skip);

  test('a file of the folder is downloaded with its name, and a path outside is refused', () async {
    final c = ChatController(api, settings);
    await api.post('/api/agent/projects', {'path': folder});
    await c.create(workspace: folder, options: NewChat(mode: 'read_only'));
    final got = await api.download('/api/agent/sessions/${c.sid}/file', {'path': 'report.txt'});
    expect(got.name, 'report.txt');
    expect(String.fromCharCodes(got.bytes), 'ciao dal PC');
    expect(() => api.download('/api/agent/sessions/${c.sid}/file', {'path': '../etc'}), throwsA(isA<ApiError>()));
    c.dispose();
  }, skip: skip);

  test('an attachment goes up as the body of a request and comes back as a card', () async {
    final c = ChatController(api, settings);
    await c.create(workspace: folder, options: NewChat(mode: 'read_only'));
    final meta = await api.upload(c.sid, 'nota.txt', 'testo'.codeUnits);
    expect(meta['name'], 'nota.txt');
    expect('${meta['id']}', isNotEmpty);
    c.dispose();
  }, skip: skip);

  test('the shared folders and their files can be listed', () async {
    await api.post('/api/agent/projects', {'path': folder});
    final roots = (await api.get('/api/files/roots'))['roots'] as List;
    expect(roots.any((r) => (r as Map)['path'].toString().toLowerCase().endsWith('progetto')), isTrue);
    final listing = await api.get('/api/files/list', query: {'path': folder});
    expect((listing['entries'] as List).map((e) => (e as Map)['name']), contains('report.txt'));
  }, skip: skip);
}
