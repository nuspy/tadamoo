import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/i18n.dart';
import 'package:basalt_mobile/src/md_save.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'secret_fakes.dart';

/// A PC that can make a Markdown file into these formats (none: an older PC).
class FormatsServer extends Api {
  FormatsServer(super.settings, {this.formats = const [], this.down = false});
  final List<Map<String, String>> formats;
  final bool down;
  final List<String> calls = [];

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    calls.add('GET $path $query');
    if (down || path != '/api/export/formats') throw ApiError(404, 'not found');
    return {'formats': formats};
  }
}

const fmts = [
  {'id': 'docx', 'label': 'Word (.docx)'},
  {'id': 'odt', 'label': 'OpenDocument text (.odt)'},
  {'id': 'html', 'label': 'Web page (.html)'},
];

void main() {
  setUp(() => Texts.use('en'));

  Future<(FormatsServer, AppSettings)> make({bool down = false, List<Map<String, String>> formats = fmts}) async {
    SharedPreferences.setMockInitialValues({});
    final settings = await AppSettings.load(secrets: FakeSecretStore());
    return (FormatsServer(settings, formats: formats, down: down), settings);
  }

  test('a Markdown file is told from other files by its name', () {
    expect(isMarkdownName('Rapporto.md'), isTrue);
    expect(isMarkdownName('Rapporto.MARKDOWN'), isTrue);
    expect(isMarkdownName('Rapporto.mdx'), isFalse);
    expect(isMarkdownName('notes.txt'), isFalse);
  });

  test('the route and query that convert a file the agent made and one in its folder', () {
    final (route, query) = mdConvertRequest('file', 's 1', 'abc123abc123', 'both', 'docx', 'it');
    expect(route, '/api/outputs/s%201/abc123abc123/convert');
    expect(query, {'format': 'docx', 'mode': 'both', 'lang': 'it'});
    final (route2, query2) = mdConvertRequest('path', 's1', 'docs/nota.md', 'converted', 'odt', 'en');
    expect(route2, '/api/agent/sessions/s1/file-convert');
    expect(query2, {'path': 'docs/nota.md', 'format': 'odt', 'mode': 'converted', 'lang': 'en'});
  });

  test('the formats come from the PC in the app\'s language, and an older PC gives none', () async {
    final (api, _) = await make();
    final list = await api.mdFormats();
    expect(list.map((f) => f.id), ['docx', 'odt', 'html']);
    expect(api.calls.single, 'GET /api/export/formats {lang: en}');
    final (old, _) = await make(down: true);
    expect(await old.mdFormats(), isEmpty);
  });

  Widget host(AppSettings s, Api api, void Function(MdSaveChoice?) got) => MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (c) => TextButton(onPressed: () async => got(await askMdSave(c, api, s, 'Report.md')), child: const Text('open')),
          ),
        ),
      );

  testWidgets('an older PC has nothing to ask: the Markdown file alone, with no sheet', (t) async {
    final (api, s) = await make(down: true);
    MdSaveChoice? got;
    await t.pumpWidget(host(s, api, (v) => got = v));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    expect(got?.mode, 'md');
    expect(find.text('Save Report.md'), findsNothing);
  });

  testWidgets('the sheet offers the three ways, starts on the Markdown alone with the format off, and saving it answers md', (t) async {
    t.view.physicalSize = const Size(360 * 3, 800 * 3);
    t.view.devicePixelRatio = 3;
    addTearDown(t.view.reset);
    final (api, s) = await make();
    MdSaveChoice? got;
    await t.pumpWidget(host(s, api, (v) => got = v));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    expect(find.text('Save Report.md'), findsOneWidget);
    expect(find.text('Only the Markdown file'), findsOneWidget);
    expect(find.text('Only the converted file'), findsOneWidget);
    expect(find.text('The Markdown file and the converted file, in one zip'), findsOneWidget);
    final dropdown = t.widget<DropdownButtonFormField<String>>(find.byType(DropdownButtonFormField<String>));
    expect(dropdown.onChanged, isNull, reason: 'the format is off while only the Markdown is chosen');
    expect(t.takeException(), isNull);
    await t.tap(find.text('Save'));
    await t.pumpAndSettle();
    expect(got?.mode, 'md');
  });

  testWidgets('the converted one and both answer with the chosen format, which is remembered for next time', (t) async {
    t.view.physicalSize = const Size(360 * 3, 800 * 3);
    t.view.devicePixelRatio = 3;
    addTearDown(t.view.reset);
    final (api, s) = await make();
    MdSaveChoice? got;
    await t.pumpWidget(host(s, api, (v) => got = v));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const ValueKey('mx-converted')));
    await t.pumpAndSettle();
    await t.tap(find.byType(DropdownButtonFormField<String>));
    await t.pumpAndSettle();
    await t.tap(find.text('OpenDocument text (.odt)').last);
    await t.pumpAndSettle();
    await t.tap(find.text('Save'));
    await t.pumpAndSettle();
    expect((got?.mode, got?.format), ('converted', 'odt'));
    expect((s.mdMode, s.mdFormat), ('converted', 'odt'));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    await t.tap(find.byKey(const ValueKey('mx-both')));
    await t.pumpAndSettle();
    await t.tap(find.text('Save'));
    await t.pumpAndSettle();
    expect((got?.mode, got?.format), ('both', 'odt'), reason: 'the format chosen last time is still there');
  });

  testWidgets('cancelling answers nothing, and every control is at least 48 high with large text', (t) async {
    t.view.physicalSize = const Size(360 * 3, 800 * 3);
    t.view.devicePixelRatio = 3;
    addTearDown(t.view.reset);
    final (api, s) = await make();
    MdSaveChoice? got = MdSaveChoice.md;
    var closed = false;
    await t.pumpWidget(MediaQuery(
      data: const MediaQueryData(size: Size(360, 800), textScaler: TextScaler.linear(1.6)),
      child: host(s, api, (v) {
        got = v;
        closed = true;
      }),
    ));
    await t.tap(find.text('open'));
    await t.pumpAndSettle();
    expect(t.takeException(), isNull);
    for (final label in const ['Cancel', 'Save']) {
      expect(t.getSize(find.ancestor(of: find.text(label), matching: find.byWidgetPredicate((w) => w is ButtonStyleButton))).height, greaterThanOrEqualTo(48));
    }
    await t.tap(find.text('Cancel'));
    await t.pumpAndSettle();
    expect(closed, isTrue);
    expect(got, isNull);
  });
}
