import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:archive/archive.dart';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api.dart';
import 'personal_pack.dart';

/// The 15 mouth shapes the PC sends in its timelines, in the order of its table.
const visemeNames = ['sil', 'PP', 'FF', 'TH', 'DD', 'kk', 'CH', 'SS', 'nn', 'RR', 'aa', 'E', 'I', 'O', 'U'];

/// What an avatar gives a phone (`GET /api/voice/avatar/<name>/manifest.json`): the mode its files allow and the SHA-256 of each file.
class AssetManifest {
  AssetManifest(this.avatar, this.renderModes, this.files, [this.engines = const {}]);

  final String avatar;
  final List<String> renderModes;
  final Map<String, String> files;

  /// What a personalised engine trained for this avatar gives the phone (`engines` of the manifest): the SHA-256 of each of its files, by path from the
  /// avatar's folder (`engines/<id>/export/...`). They are not in `files` and not in the zip.
  final Map<String, Map<String, String>> engines;

  /// Null when the text is not a manifest this build understands.
  static AssetManifest? parse(Uint8List bytes) {
    try {
      final j = jsonDecode(utf8.decode(bytes));
      if (j is! Map || j['version'] != 1 || j['files'] is! Map) return null;
      final files = <String, String>{};
      for (final e in (j['files'] as Map).entries) {
        final path = '${e.key}';
        final sum = '${e.value}'.toLowerCase();
        if (path.isEmpty || path.startsWith('/') || path.contains('..') || path.contains('\\') || !RegExp(r'^[0-9a-f]{64}$').hasMatch(sum)) return null;
        files[path] = sum;
      }
      final engines = <String, Map<String, String>>{};
      final raw = j['engines'];
      if (raw is Map) {
        for (final e in raw.entries) {
          final id = '${e.key}';
          final v = e.value;
          if (!personalEngineIds.contains(id) || v is! Map || v['files'] is! Map) continue;
          final own = <String, String>{};
          var bad = false;
          for (final f in (v['files'] as Map).entries) {
            final path = '${f.key}';
            final sum = '${f.value}'.toLowerCase();
            if (!path.startsWith('engines/$id/export/') || !PersonalPackInfo.safe(path) || !RegExp(r'^[0-9a-f]{64}$').hasMatch(sum)) bad = true;
            own[path] = sum;
          }
          if (!bad && own.containsKey('engines/$id/export/phone.json')) engines[id] = own;
        }
      }
      return AssetManifest('${j['avatar'] ?? ''}', [for (final m in (j['render_modes'] as List? ?? const [])) '$m'], files, engines);
    } catch (_) {
      return null;
    }
  }

  static const visemeFiles = ['poster.jpg', 'visemes/frame.json'];
  static const wav2lipFiles = ['poster.jpg', 'wav2lip/face.png', 'wav2lip/box.json'];

  /// The files the viseme renderer needs: the photo, the frame and the 15 sprites.
  List<String> get forVisemes => [...visemeFiles, for (final v in visemeNames) 'visemes/$v.png'];

  bool get hasVisemes => forVisemes.every(files.containsKey);
  bool get hasWav2lip => wav2lipFiles.every(files.containsKey);

  /// Bytes of every file listed is not known; the number of files is.
  int get count => files.length;
}

/// Where the phone's files come from. The real one is the PC's server with this phone's key; the tests use a stand-in.
abstract class AssetSource {
  /// A small file whole: the manifest or the zip.
  Future<Uint8List> bytes(String path);

  /// The Wav2Lip model into `part`, continuing it when it is already partly there; `etag` is the SHA-256 the PC states for the file it served.
  Future<ModelFetch> model(String path, File part, {required void Function(int got, int total) progress});
}

class ModelFetch {
  ModelFetch(this.etag, this.total);
  final String etag;
  final int total;
}

class AssetError implements Exception {
  AssetError(this.reason, [this.detail = '']);

  /// `network`, `manifest`, `zip`, `sha`, `missing`, `not_installed`, `size` or `disk`.
  final String reason;
  final String detail;
  @override
  String toString() => detail.isEmpty ? reason : '$reason: $detail';
}

/// The PC's routes for the avatar's files, over the address and key the app already uses.
class HttpAssetSource implements AssetSource {
  HttpAssetSource(this.api, {http.Client? client}) : _client = client ?? http.Client();
  final Api api;
  final http.Client _client;

  @override
  Future<Uint8List> bytes(String path) async {
    try {
      final r = await _client.get(api.uri(path), headers: api.headers);
      if (r.statusCode == 404) throw AssetError('missing', path);
      if (r.statusCode >= 400) throw AssetError('network', '${r.statusCode}');
      return r.bodyBytes;
    } on SocketException {
      throw AssetError('network');
    } on http.ClientException {
      throw AssetError('network');
    }
  }

  @override
  Future<ModelFetch> model(String path, File part, {required void Function(int got, int total) progress}) async {
    final tag = File('${part.path}.etag');
    var have = part.existsSync() ? part.lengthSync() : 0;
    final known = tag.existsSync() ? tag.readAsStringSync().trim() : '';
    final request = http.Request('GET', api.uri(path))..headers.addAll(api.headers);
    if (have > 0 && known.isNotEmpty) {
      request.headers['Range'] = 'bytes=$have-';
      request.headers['If-Range'] = '"$known"';
    } else {
      have = 0;
    }
    http.StreamedResponse r;
    try {
      r = await _client.send(request);
    } on SocketException {
      throw AssetError('network');
    } on http.ClientException {
      throw AssetError('network');
    }
    if (r.statusCode == 404) throw AssetError('not_installed');
    if (r.statusCode != 200 && r.statusCode != 206) throw AssetError('network', '${r.statusCode}');
    final etag = (r.headers['etag'] ?? '').replaceAll('"', '').replaceAll('W/', '').toLowerCase();
    final resumed = r.statusCode == 206;
    if (!resumed) have = 0;
    final total = resumed ? _totalOf(r.headers['content-range']) ?? have + (r.contentLength ?? 0) : (r.contentLength ?? 0);
    part.parent.createSync(recursive: true);
    if (etag.isNotEmpty) tag.writeAsStringSync(etag);
    final sink = part.openWrite(mode: resumed ? FileMode.append : FileMode.write);
    var got = have;
    try {
      await for (final chunk in r.stream) {
        sink.add(chunk);
        got += chunk.length;
        progress(got, total);
      }
      await sink.flush();
    } on SocketException {
      throw AssetError('network');
    } on http.ClientException {
      throw AssetError('network');
    } finally {
      await sink.close();
    }
    return ModelFetch(etag, total);
  }

  static int? _totalOf(String? contentRange) {
    final m = RegExp(r'/(\d+)$').firstMatch(contentRange ?? '');
    return m == null ? null : int.parse(m.group(1)!);
  }
}

/// How an avatar's files stand on this phone.
enum AssetStage { unknown, missing, downloading, ready, failed }

/// The Wav2Lip file this build knows: the one the PC keeps in its catalog as `wav2lip-int8`. A file with another SHA-256 is refused,
/// because the pipeline is written for this one.
const wav2lipModelSha = '2e297b310dcd397ac94e467814570eaf18edec322c29abd14565dcd3b6896f97';
const wav2lipModelBytes = 36664106;

enum ModelStage { missing, downloading, ready, failed }

/// The avatar's files and the Wav2Lip model on the phone: what is there, what was verified, and the downloads. Files are written under
/// `root/avatars/<name>/` only after the SHA-256 of every one matched the manifest, so what is there is whole.
class AvatarAssetStore extends ChangeNotifier {
  AvatarAssetStore({required this.root, required this.source, this.modelSha = wav2lipModelSha, this.modelBytes = wav2lipModelBytes}) : modelTotal = modelBytes;

  final Directory root;
  final AssetSource source;

  /// The SHA-256 and the size of the model file this phone accepts.
  final String modelSha;
  final int modelBytes;

  final Map<String, AssetStage> _stages = {};
  final Map<String, AssetManifest> _manifests = {};
  final Map<String, EngineStage> _engineStages = {};
  final Map<String, PersonalPackInfo> _packs = {};
  final Map<String, PersonalTestRecord> _packTests = {};
  String engineReason = '';
  int engineGot = 0;
  int engineTotal = 0;
  String lastReason = '';
  ModelStage modelStage = ModelStage.missing;
  int modelGot = 0;
  int modelTotal;
  String modelReason = '';
  bool _disposed = false;

  static const modelPath = '/api/voice/avatar/models/wav2lip.onnx';

  Directory dirOf(String name) => Directory('${root.path}/avatars/$name');
  String pathOf(String name, String rel) => '${dirOf(name).path}/$rel';
  File get modelFile => File('${root.path}/models/wav2lip_int8.onnx');
  File get _modelPart => File('${root.path}/models/wav2lip_int8.onnx.part');

  static bool validName(String name) => RegExp(r'^[A-Za-z0-9_-]{1,64}$').hasMatch(name);

  AssetStage stageOf(String name) => _stages[name] ?? AssetStage.unknown;
  AssetManifest? manifestOf(String name) => _manifests[name];

  String _hex(List<int> bytes) => sha256.convert(bytes).toString();

  /// Reads what is on disk for `name`: its saved manifest and every file it lists, each one hashed. True when all are there and match.
  Future<bool> refresh(String name) async {
    if (!validName(name)) return false;
    final file = File(pathOf(name, 'manifest.json'));
    AssetManifest? m;
    if (file.existsSync()) m = AssetManifest.parse(await file.readAsBytes());
    var ok = m != null && (m.hasVisemes || m.hasWav2lip);
    if (ok) {
      for (final e in m.files.entries) {
        final f = File(pathOf(name, e.key));
        if (!f.existsSync() || _hex(await f.readAsBytes()) != e.value) {
          ok = false;
          break;
        }
      }
    }
    if (ok) _manifests[name] = m!;
    _set(name, ok ? AssetStage.ready : AssetStage.missing);
    return ok;
  }

  /// Fetches the manifest and, when something differs from what is on disk, the zip; keeps only files whose SHA-256 is the manifest's.
  /// Returns true when the avatar is whole on the phone afterwards; `lastReason` says why not.
  Future<bool> sync(String name) async {
    if (!validName(name)) {
      lastReason = 'manifest';
      return false;
    }
    _set(name, AssetStage.downloading);
    try {
      final m = AssetManifest.parse(await source.bytes('/api/voice/avatar/$name/manifest.json'));
      if (m == null) throw AssetError('manifest');
      if (!m.hasVisemes && !m.hasWav2lip) throw AssetError('missing', 'no mode');
      final need = <String>[];
      for (final e in m.files.entries) {
        final f = File(pathOf(name, e.key));
        if (!f.existsSync() || _hex(await f.readAsBytes()) != e.value) need.add(e.key);
      }
      if (need.isNotEmpty) {
        final zip = await source.bytes('/api/voice/avatar/$name/assets.zip');
        Archive archive;
        try {
          archive = ZipDecoder().decodeBytes(zip);
        } catch (_) {
          throw AssetError('zip');
        }
        final got = {for (final f in archive.files) if (f.isFile) f.name: f.content};
        for (final rel in need) {
          final data = got[rel];
          if (data == null) throw AssetError('zip', rel);
          if (_hex(data) != m.files[rel]) throw AssetError('sha', rel);
        }
        for (final rel in need) {
          final f = File(pathOf(name, rel));
          await f.parent.create(recursive: true);
          await f.writeAsBytes(got[rel]!, flush: true);
        }
      }
      await File(pathOf(name, 'manifest.json')).writeAsBytes(await _manifestBytes(m), flush: true);
      _manifests[name] = m;
      lastReason = '';
      _set(name, AssetStage.ready);
      return true;
    } on AssetError catch (e) {
      lastReason = e.reason;
    } on FileSystemException {
      lastReason = 'disk';
    }
    _set(name, AssetStage.failed);
    return false;
  }

  Future<Uint8List> _manifestBytes(AssetManifest m) async => Uint8List.fromList(utf8.encode(jsonEncode({
        'version': 1,
        'avatar': m.avatar,
        'render_modes': m.renderModes,
        'files': m.files,
        if (m.engines.isNotEmpty) 'engines': {for (final e in m.engines.entries) e.key: {'files': e.value}},
      })));

  // ---- the personalised engines (Ultralight, FeatherTalk)

  String _enginePrefix(String id) => 'engines/$id/export';

  /// The folder of an engine's package for `name`.
  String engineDir(String name, String id) => pathOf(name, _enginePrefix(id));

  EngineStage engineStageOf(String name, String id) => _engineStages['$name/$id'] ?? EngineStage.missing;

  /// The package's `phone.json` once every file of it was verified on this phone; null before.
  PersonalPackInfo? packOf(String name, String id) => engineStageOf(name, id) == EngineStage.ready ? _packs['$name/$id'] : null;

  /// The SHA-256 of `phone.json` the avatar's manifest states for `id`, or null when the avatar has no such engine.
  String? phoneShaOf(String name, String id) => _manifests[name]?.engines[id]?['${_enginePrefix(id)}/phone.json'];

  /// Whether the avatar's manifest offers this engine at all.
  bool offersEngine(String name, String id) => phoneShaOf(name, id) != null;

  PersonalTestRecord? personalTestOf(String name, String id) => _packTests['$name/$id'];

  /// Whether the package on the phone is the one that passed the self-test here.
  bool personalPassed(String name, String id) {
    final t = _packTests['$name/$id'];
    final sha = phoneShaOf(name, id);
    return t != null && sha != null && t.sha == sha;
  }

  File _testFile(String name, String id) => File('${dirOf(name).path}/engines/$id/selftest.json');

  /// Keeps the outcome of a self-test (`sha` is the `phone.json` that passed; '' forgets it).
  Future<void> savePersonalTest(String name, String id, PersonalTestRecord record) async {
    final f = _testFile(name, id);
    try {
      if (record.sha.isEmpty) {
        _packTests.remove('$name/$id');
        if (f.existsSync()) f.deleteSync();
      } else {
        _packTests['$name/$id'] = record;
        await f.parent.create(recursive: true);
        await f.writeAsString(record.encode(), flush: true);
      }
    } on FileSystemException {
      // the result is kept for this run only
    }
    _tell();
  }

  /// Reads what is on disk for an engine's package: `phone.json` with the SHA-256 the manifest states, and every file it lists with its size and SHA-256.
  Future<bool> refreshEngine(String name, String id) async {
    final key = '$name/$id';
    if (_engineStages[key] == EngineStage.downloading) return false;
    final entry = validName(name) ? _manifests[name]?.engines[id] : null;
    PersonalPackInfo? info;
    if (entry != null) {
      final f = File('${engineDir(name, id)}/phone.json');
      final want = entry['${_enginePrefix(id)}/phone.json'];
      if (f.existsSync() && await _fileSha(f) == want) {
        info = PersonalPackInfo.parse(await f.readAsBytes(), id);
        if (info != null && !await _packFilesOk(name, id, entry, info)) info = null;
      }
    }
    if (info == null) {
      _packs.remove(key);
      _engineStages[key] = EngineStage.missing;
    } else {
      _packs[key] = info;
      _engineStages[key] = EngineStage.ready;
      final t = _testFile(name, id);
      final record = t.existsSync() ? PersonalTestRecord.decode(await t.readAsString()) : null;
      if (record != null) _packTests[key] = record;
    }
    _tell();
    return info != null;
  }

  Future<bool> _packFilesOk(String name, String id, Map<String, String> entry, PersonalPackInfo info) async {
    for (final e in info.files.entries) {
      if (entry['${_enginePrefix(id)}/${e.key}'] != e.value.sha256) return false;
      final f = File('${engineDir(name, id)}/${e.key}');
      if (!f.existsSync() || f.lengthSync() != e.value.bytes || await _fileSha(f) != e.value.sha256) return false;
    }
    return true;
  }

  /// Fetches the files of an engine's package that are missing or differ, one by one with resume, and keeps each only when its size and SHA-256 are
  /// the ones `phone.json` says and the avatar's manifest agrees. True when the package is whole afterwards; `engineReason` says why not.
  Future<bool> syncEngine(String name, String id) async {
    final key = '$name/$id';
    final entry = validName(name) ? _manifests[name]?.engines[id] : null;
    if (entry == null) {
      engineReason = 'manifest';
      return false;
    }
    if (_engineStages[key] == EngineStage.downloading) return false;
    _engineStages[key] = EngineStage.downloading;
    engineGot = 0;
    engineTotal = 0;
    engineReason = '';
    _tell();
    final base = '/api/voice/avatar/$name/engines/$id/files';
    try {
      final phoneFile = File('${engineDir(name, id)}/phone.json');
      final want = entry['${_enginePrefix(id)}/phone.json']!;
      if (!phoneFile.existsSync() || await _fileSha(phoneFile) != want) {
        final bytes = await source.bytes('$base/phone.json');
        if (_hex(bytes) != want) throw AssetError('sha', 'phone.json');
        await phoneFile.parent.create(recursive: true);
        await phoneFile.writeAsBytes(bytes, flush: true);
      }
      final info = PersonalPackInfo.parse(await phoneFile.readAsBytes(), id);
      if (info == null) throw AssetError('manifest', 'phone.json');
      for (final e in info.files.entries) {
        if (entry['${_enginePrefix(id)}/${e.key}'] != e.value.sha256) throw AssetError('manifest', e.key);
      }
      engineTotal = info.bytes;
      var done = 0;
      for (final e in info.files.entries) {
        final target = File('${engineDir(name, id)}/${e.key}');
        if (target.existsSync() && target.lengthSync() == e.value.bytes && await _fileSha(target) == e.value.sha256) {
          done += e.value.bytes;
          continue;
        }
        final part = File('${target.path}.part');
        await part.parent.create(recursive: true);
        final before = done;
        final fetch = await source.model('$base/${e.key}', part, progress: (got, total) {
          engineGot = before + got;
          _tell();
        });
        if (part.lengthSync() != e.value.bytes || await _fileSha(part) != e.value.sha256 || (fetch.etag.isNotEmpty && fetch.etag != e.value.sha256)) {
          for (final f in [part, File('${part.path}.etag')]) {
            if (f.existsSync()) f.deleteSync();
          }
          throw AssetError('sha', e.key);
        }
        if (target.existsSync()) target.deleteSync();
        part.renameSync(target.path);
        final tag = File('${part.path}.etag');
        if (tag.existsSync()) tag.deleteSync();
        done += e.value.bytes;
      }
      _engineStages[key] = EngineStage.missing;
      final ok = await refreshEngine(name, id);
      if (!ok) engineReason = 'sha';
      return ok;
    } on AssetError catch (e) {
      engineReason = e.reason;
    } on FileSystemException {
      engineReason = 'disk';
    }
    _engineStages[key] = EngineStage.failed;
    _tell();
    return false;
  }

  // ---- the Wav2Lip model

  /// Whether the model file is on the phone with the size and SHA-256 of the one this build runs.
  Future<bool> refreshModel() async {
    final f = modelFile;
    if (f.existsSync() && f.lengthSync() == modelBytes && await _fileSha(f) == modelSha) {
      modelStage = ModelStage.ready;
    } else if (modelStage != ModelStage.downloading) {
      modelStage = ModelStage.missing;
    }
    _tell();
    return modelStage == ModelStage.ready;
  }

  Future<String> _fileSha(File f) async => (await sha256.bind(f.openRead()).first).toString();

  /// Downloads the model, continuing a partial file, and keeps it only if its SHA-256 is the one the PC states and the one this build knows.
  Future<bool> downloadModel() async {
    if (modelStage == ModelStage.downloading) return false;
    modelStage = ModelStage.downloading;
    modelGot = 0;
    modelReason = '';
    _tell();
    try {
      final fetch = await source.model(modelPath, _modelPart, progress: (got, total) {
        modelGot = got;
        if (total > 0) modelTotal = total;
        _tell();
      });
      final part = _modelPart;
      if (part.lengthSync() != modelBytes) {
        part.deleteSync();
        throw AssetError('size');
      }
      final sum = await _fileSha(part);
      if (sum != modelSha || (fetch.etag.isNotEmpty && fetch.etag != sum)) {
        part.deleteSync();
        final stale = File('${part.path}.etag');
        if (stale.existsSync()) stale.deleteSync();
        throw AssetError('sha');
      }
      if (modelFile.existsSync()) modelFile.deleteSync();
      part.renameSync(modelFile.path);
      final tag = File('${part.path}.etag');
      if (tag.existsSync()) tag.deleteSync();
      modelStage = ModelStage.ready;
      _tell();
      return true;
    } on AssetError catch (e) {
      modelReason = e.reason;
    } on FileSystemException {
      modelReason = 'disk';
    }
    modelStage = ModelStage.failed;
    _tell();
    return false;
  }

  /// Takes the model off the phone (and a partial download of it).
  Future<void> removeModel() async {
    for (final f in [modelFile, _modelPart, File('${_modelPart.path}.etag')]) {
      if (f.existsSync()) f.deleteSync();
    }
    modelStage = ModelStage.missing;
    _tell();
  }

  void _set(String name, AssetStage s) {
    _stages[name] = s;
    _tell();
  }

  void _tell() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}
