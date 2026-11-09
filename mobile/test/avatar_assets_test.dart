import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/avatar_assets.dart';
import 'package:basalt_mobile/src/settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'render_fakes.dart';

void main() {
  late Directory dir;
  late FakeAssetSource source;
  late AvatarAssetStore store;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('basalt_assets');
    source = FakeAssetSource();
    store = AvatarAssetStore(root: dir, source: source, modelSha: sha(utf8.encode('the model')), modelBytes: 9);
  });

  tearDown(() {
    store.dispose();
    dir.deleteSync(recursive: true);
  });

  group('the manifest', () {
    test('it lists the files with their SHA-256 and says which modes they allow', () {
      final m = AssetManifest.parse(FakeAvatarFiles('ada').manifest)!;
      expect(m.avatar, 'ada');
      expect(m.hasVisemes, isTrue);
      expect(m.hasWav2lip, isTrue);
      expect(m.forVisemes.length, 17);
      final only = AssetManifest.parse(FakeAvatarFiles('ada', wav2lip: false).manifest)!;
      expect(only.hasVisemes, isTrue);
      expect(only.hasWav2lip, isFalse);
    });

    test('a manifest that is not one, or has a path that goes out of the folder, or a bad hash, is refused', () {
      Uint8List j(Object o) => Uint8List.fromList(utf8.encode(jsonEncode(o)));
      expect(AssetManifest.parse(Uint8List.fromList([1, 2, 3])), isNull);
      expect(AssetManifest.parse(j({'version': 2, 'files': {}})), isNull);
      expect(AssetManifest.parse(j({'version': 1, 'files': {'../x': 'a' * 64}})), isNull);
      expect(AssetManifest.parse(j({'version': 1, 'files': {'/x': 'a' * 64}})), isNull);
      expect(AssetManifest.parse(j({'version': 1, 'files': {'poster.jpg': 'nothex'}})), isNull);
    });
  });

  group('the avatar\'s files', () {
    test('they are fetched through the manifest and the zip, and kept only when every SHA-256 matches', () async {
      final a = FakeAvatarFiles('ada');
      source.serve(a);
      expect(store.stageOf('ada'), AssetStage.unknown);
      expect(await store.sync('ada'), isTrue);
      expect(store.stageOf('ada'), AssetStage.ready);
      expect(source.asked, ['/api/voice/avatar/ada/manifest.json', '/api/voice/avatar/ada/assets.zip']);
      for (final e in a.files.entries) {
        expect(File(store.pathOf('ada', e.key)).readAsBytesSync(), e.value, reason: e.key);
      }
      expect(await store.refresh('ada'), isTrue);
    });

    test('files that are already there and match are not fetched again', () async {
      source.serve(FakeAvatarFiles('ada'));
      await store.sync('ada');
      source.asked.clear();
      expect(await store.sync('ada'), isTrue);
      expect(source.asked, ['/api/voice/avatar/ada/manifest.json']);
    });

    test('a file that changed on the PC is fetched again and the others are kept', () async {
      final a = FakeAvatarFiles('ada');
      source.serve(a);
      await store.sync('ada');
      a.files['visemes/aa.png'] = Uint8List.fromList(utf8.encode('new aa'));
      source.serve(a);
      expect(await store.sync('ada'), isTrue);
      expect(File(store.pathOf('ada', 'visemes/aa.png')).readAsStringSync(), 'new aa');
    });

    test('a file whose SHA-256 is not the manifest\'s makes the whole download fail and nothing is written', () async {
      final a = FakeAvatarFiles('ada');
      source.serve(a);
      final bad = FakeAvatarFiles('ada')..files['visemes/O.png'] = Uint8List.fromList(utf8.encode('tampered'));
      source.routes['/api/voice/avatar/ada/assets.zip'] = bad.zip;
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'sha');
      expect(store.stageOf('ada'), AssetStage.failed);
      expect(Directory(store.pathOf('ada', '')).existsSync() && File(store.pathOf('ada', 'poster.jpg')).existsSync(), isFalse);
    });

    test('a zip with a file missing, a zip that is not one, a manifest that is not one and a PC that does not answer each say why', () async {
      final a = FakeAvatarFiles('ada');
      source.serve(a);
      final short = FakeAvatarFiles('ada')..files.remove('visemes/U.png');
      source.routes['/api/voice/avatar/ada/assets.zip'] = short.zip;
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'zip');
      source.routes['/api/voice/avatar/ada/assets.zip'] = Uint8List.fromList([9, 9, 9, 9]);
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'zip');
      source.routes['/api/voice/avatar/ada/manifest.json'] = Uint8List.fromList([1]);
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'manifest');
      source.offline = true;
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'network');
      source.offline = false;
      expect(await store.sync('nobody'), isFalse);
      expect(store.lastReason, 'missing');
    });

    test('a name that could leave the folder is refused', () async {
      expect(await store.sync('../x'), isFalse);
      expect(await store.refresh('a/b'), isFalse);
      expect(AvatarAssetStore.validName('ada_2-b'), isTrue);
      expect(AvatarAssetStore.validName(''), isFalse);
    });

    test('a file damaged on the phone is noticed when the folder is read again', () async {
      source.serve(FakeAvatarFiles('ada'));
      await store.sync('ada');
      File(store.pathOf('ada', 'visemes/PP.png')).writeAsStringSync('rotten');
      expect(await store.refresh('ada'), isFalse);
      expect(store.stageOf('ada'), AssetStage.missing);
    });

    test('an avatar with neither mode in its manifest is not usable', () async {
      final a = FakeAvatarFiles('ada', wav2lip: false, visemes: false);
      source.serve(a);
      expect(await store.sync('ada'), isFalse);
      expect(store.lastReason, 'missing');
    });
  });

  group('the Wav2Lip model', () {
    test('it is kept when its size and SHA-256 are the ones this build knows and the PC\'s own', () async {
      source.modelBytes = Uint8List.fromList(utf8.encode('the model'));
      expect(await store.refreshModel(), isFalse);
      expect(store.modelStage, ModelStage.missing);
      expect(await store.downloadModel(), isTrue);
      expect(store.modelStage, ModelStage.ready);
      expect(store.modelFile.existsSync(), isTrue);
      expect(File('${store.modelFile.path}.part').existsSync(), isFalse);
      expect(await store.refreshModel(), isTrue);
    });

    test('a file with another SHA-256 is thrown away, and so is one the PC says is another', () async {
      source.modelBytes = Uint8List.fromList(utf8.encode('the modeX'));
      expect(await store.downloadModel(), isFalse);
      expect(store.modelReason, 'sha');
      expect(store.modelStage, ModelStage.failed);
      expect(store.modelFile.existsSync(), isFalse);
      source.modelBytes = Uint8List.fromList(utf8.encode('the model'));
      source.modelEtag = 'f' * 64;
      expect(await store.downloadModel(), isFalse);
      expect(store.modelReason, 'sha');
    });

    test('a file of the wrong size, a model the PC does not have and no network each say why', () async {
      source.modelBytes = Uint8List.fromList(utf8.encode('too long a model'));
      expect(await store.downloadModel(), isFalse);
      expect(store.modelReason, 'size');
      source.modelMissing = true;
      expect(await store.downloadModel(), isFalse);
      expect(store.modelReason, 'not_installed');
      source.offline = true;
      expect(await store.downloadModel(), isFalse);
      expect(store.modelReason, 'network');
    });

    test('the model can be taken off the phone', () async {
      source.modelBytes = Uint8List.fromList(utf8.encode('the model'));
      await store.downloadModel();
      await store.removeModel();
      expect(store.modelFile.existsSync(), isFalse);
      expect(store.modelStage, ModelStage.missing);
    });
  });

  group('the download over HTTP', () {
    late Api api;
    final body = Uint8List.fromList(List.generate(1000, (i) => i % 251));

    setUp(() async {
      SharedPreferences.setMockInitialValues({'flutter.address': '100.64.1.2:7232', 'flutter.key': 'bsk_phone'});
      api = Api(await AppSettings.load());
    });

    test('small files carry the phone\'s key; a 404 is "missing"', () async {
      final seen = <http.BaseRequest>[];
      final src = HttpAssetSource(api, client: MockClient((r) async {
        seen.add(r);
        return r.url.path.endsWith('nothing') ? http.Response('', 404) : http.Response.bytes([1, 2, 3], 200);
      }));
      expect(await src.bytes('/api/voice/avatar/ada/manifest.json'), [1, 2, 3]);
      expect(seen.single.headers['Authorization'], 'Bearer bsk_phone');
      expect(seen.single.url.toString(), 'http://100.64.1.2:7232/api/voice/avatar/ada/manifest.json');
      await expectLater(src.bytes('/api/voice/avatar/x/nothing'), throwsA(isA<AssetError>().having((e) => e.reason, 'reason', 'missing')));
    });

    test('the model is written to the partial file, with the PC\'s ETag as the hash it states', () async {
      final part = File('${dir.path}/m.part');
      final src = HttpAssetSource(api, client: MockClient.streaming((req, _) async => http.StreamedResponse(Stream.value(body), 200, contentLength: body.length, headers: {'etag': '"abc"'})));
      var last = 0;
      final fetch = await src.model('/api/voice/avatar/models/wav2lip.onnx', part, progress: (got, total) => last = got);
      expect(fetch.etag, 'abc');
      expect(part.readAsBytesSync(), body);
      expect(last, 1000);
    });

    test('a partial file is continued with a Range request and If-Range, and the rest is appended', () async {
      final part = File('${dir.path}/m.part')..writeAsBytesSync(body.sublist(0, 400));
      File('${part.path}.etag').writeAsStringSync('abc');
      http.BaseRequest? seen;
      final src = HttpAssetSource(api, client: MockClient.streaming((req, _) async {
        seen = req;
        return http.StreamedResponse(Stream.value(body.sublist(400)), 206, contentLength: 600, headers: {'etag': '"abc"', 'content-range': 'bytes 400-999/1000'});
      }));
      final fetch = await src.model('/api/voice/avatar/models/wav2lip.onnx', part, progress: (_, _) {});
      expect(seen!.headers['Range'], 'bytes=400-');
      expect(seen!.headers['If-Range'], '"abc"');
      expect(part.readAsBytesSync(), body);
      expect(fetch.total, 1000);
    });

    test('when the PC answers the whole file to a Range request the partial file is started again', () async {
      final part = File('${dir.path}/m.part')..writeAsBytesSync(Uint8List(400));
      File('${part.path}.etag').writeAsStringSync('old');
      final src = HttpAssetSource(api, client: MockClient.streaming((req, _) async => http.StreamedResponse(Stream.value(body), 200, contentLength: 1000, headers: {'etag': '"new"'})));
      await src.model('/api/voice/avatar/models/wav2lip.onnx', part, progress: (_, _) {});
      expect(part.readAsBytesSync(), body);
    });

    test('404 is "not installed" and another error is a network failure', () async {
      final part = File('${dir.path}/m.part');
      var code = 404;
      final src = HttpAssetSource(api, client: MockClient.streaming((req, _) async => http.StreamedResponse(const Stream.empty(), code)));
      await expectLater(src.model('/m', part, progress: (_, _) {}), throwsA(isA<AssetError>().having((e) => e.reason, 'reason', 'not_installed')));
      code = 500;
      await expectLater(src.model('/m', part, progress: (_, _) {}), throwsA(isA<AssetError>().having((e) => e.reason, 'reason', 'network')));
    });
  });
}
