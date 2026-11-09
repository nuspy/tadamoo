import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' show Rect, Size;

/// The personalised engines that have a runtime on the phone: an avatar trained for one person and packaged by the PC
/// (voice/avatar_engines/phone_export.py). Ultralight with HuBERT Large cannot run on a phone and is never packaged, so it never reaches this list.
const personalEngineIds = ['ultralight', 'feathertalk'];

/// How an engine's package stands on this phone.
enum EngineStage { missing, downloading, ready, failed }

/// What `phone.json` of a package says, read strictly: a field of the wrong kind makes the whole package unusable.
class PersonalPackInfo {
  PersonalPackInfo._({
    required this.engine,
    required this.fps,
    required this.size,
    required this.kind,
    required this.frameFile,
    required this.frameSize,
    required this.inv,
    required this.mask,
    required this.files,
    required this.stages,
  });

  final String engine;

  /// The video rate the model was trained at, which is the rate of the frames it draws.
  final int fps;

  /// The side of the face the model draws, in pixels.
  final int size;

  /// The audio front end: `feather_hubert` or `wenet`.
  final String kind;

  /// The template frame the face is drawn over, relative to the package, and its size in pixels.
  final String frameFile;
  final Size frameSize;

  /// From the drawn face's pixels to the template frame's: x' = a x + b y + c, y' = d x + e y + f.
  final List<double> inv;

  /// The rectangle of the drawn face the model was asked to redraw.
  final Rect mask;

  /// `{path in the package: (sha256, bytes)}`.
  final Map<String, ({String sha256, int bytes})> files;
  final int stages;

  static final _hex = RegExp(r'^[0-9a-f]{64}$');

  static bool safe(String path) => path.isNotEmpty && !path.startsWith('/') && !path.contains('..') && !path.contains('\\');

  /// Null when the text is not a `phone.json` of version 1 for `engine`.
  static PersonalPackInfo? parse(Uint8List bytes, String engine) {
    try {
      final j = jsonDecode(utf8.decode(bytes));
      if (j is! Map || j['version'] != 1 || j['engine'] != engine || !personalEngineIds.contains(engine)) return null;
      final fps = j['fps'];
      final size = j['size'];
      final front = j['frontend'];
      final t = j['template'];
      final test = j['selftest'];
      if (fps is! int || fps < 1 || fps > 60 || size is! int || size < 32 || size > 512 || front is! Map || t is! Map || test is! Map || j['files'] is! Map) return null;
      final kind = '${front['kind']}';
      if (kind != 'feather_hubert' && kind != 'wenet') return null;
      final stages = test['stages'];
      if (stages is! List || stages.isEmpty || test['tolerance'] is! Map) return null;
      final frame = '${t['frame']}';
      final inv = [for (final v in (t['inv'] as List)) (v as num).toDouble()];
      final m = t['mask'] as Map;
      final mask = Rect.fromLTWH((m['x'] as num).toDouble(), (m['y'] as num).toDouble(), (m['w'] as num).toDouble(), (m['h'] as num).toDouble());
      final w = (t['frame_w'] as num).toDouble();
      final h = (t['frame_h'] as num).toDouble();
      if (!safe(frame) || inv.length != 6 || mask.width <= 0 || mask.height <= 0 || w <= 0 || h <= 0) return null;
      final files = <String, ({String sha256, int bytes})>{};
      for (final e in (j['files'] as Map).entries) {
        final meta = e.value;
        if (!safe('${e.key}') || meta is! Map || !_hex.hasMatch('${meta['sha256']}') || meta['bytes'] is! int || (meta['bytes'] as int) < 0) return null;
        files['${e.key}'] = (sha256: '${meta['sha256']}', bytes: meta['bytes'] as int);
      }
      if (!files.containsKey(frame) || !files.containsKey('${t['input']}')) return null;
      return PersonalPackInfo._(engine: engine, fps: fps, size: size, kind: kind, frameFile: frame, frameSize: Size(w, h), inv: inv, mask: mask, files: files, stages: stages.length);
    } catch (_) {
      return null;
    }
  }

  /// Bytes of all the files of the package.
  int get bytes => files.values.fold(0, (a, f) => a + f.bytes);
}

/// The outcome of a package's self-test on this phone: which `phone.json` (its SHA-256) passed and how fast it ran.
class PersonalTestRecord {
  const PersonalTestRecord(this.sha, this.fps, this.ms);
  final String sha;
  final double fps;
  final double ms;

  String encode() => jsonEncode({'sha': sha, 'fps': fps, 'ms': ms});

  static PersonalTestRecord? decode(String text) {
    try {
      final j = jsonDecode(text);
      if (j is! Map || j['sha'] is! String) return null;
      return PersonalTestRecord(j['sha'] as String, (j['fps'] as num?)?.toDouble() ?? 0, (j['ms'] as num?)?.toDouble() ?? 0);
    } catch (_) {
      return null;
    }
  }
}
