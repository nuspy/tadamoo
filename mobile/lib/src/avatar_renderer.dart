import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'avatar_assets.dart';
import 'avatar_devices.dart';
import 'viseme_timeline.dart';

/// An avatar drawn on the phone. The controller tells it what the PC sends (the start of a reply, a timeline, the voice, the end, an
/// interruption) and the screen shows `view()`. A renderer that cannot go on calls `onFailed` with the reason that goes to the PC.
abstract class AvatarRenderer extends ChangeNotifier {
  /// The render mode of the gateway it draws: `visemes` or `wav2lip`.
  String get mode;

  /// Called once, with a short reason (`too_slow`, `thermal`, `inference_failed`, ...), when this renderer cannot go on.
  void Function(String why)? onFailed;

  void audioStart(int sampleRate) {}
  void visemes(Map<String, dynamic> message) {}
  void audio(Uint8List pcm16) {}
  void audioEnd() {}

  /// An interrupt, a new answer or a lost connection: everything not yet shown is dropped and the mouth rests.
  void flush() {}

  /// One step of the screen's clock (about 30 a second while shown).
  void tick() {}

  /// The picture of the whole avatar, to put in the tab.
  Widget view() => AvatarFaceView(renderer: this);

  /// What the painter needs.
  FaceScene? get scene;

  /// The slow movement of the picture at rest; off when the phone asks for reduced motion.
  bool reducedMotion = false;

  void fail(String why) {
    final f = onFailed;
    onFailed = null;
    f?.call(why);
  }
}

/// The photo and what is laid over it at this instant, in the photo's own pixels.
class FaceScene {
  FaceScene({required this.poster, required this.posterSize, required this.motion, required this.layers});

  final ui.Image poster;
  final Size posterSize;
  final (double, double, double, double) motion;

  /// Pictures drawn over the poster, in order: each has its own 2x3 matrix from its pixels to the poster's.
  final List<FaceLayer> layers;
}

class FaceLayer {
  FaceLayer(this.image, this.inv, this.size, {this.opacity = 1, this.clip});

  final ui.Image image;

  /// `[a, b, c, d, e, f]`: x' = a x + b y + c, y' = d x + e y + f (the convention of `box.json` and `frame.json`).
  final List<double> inv;
  final Size size;
  final double opacity;

  /// A rectangle of the layer's pixels outside which nothing is drawn (the mouth of the Wav2Lip face), softened at its edge.
  final Rect? clip;
}

/// The files of the viseme mode, decoded: the photo, the 15 sprites and the frame that places them.
class VisemePack {
  VisemePack({required this.poster, required this.posterSize, required this.sprites, required this.inv, required this.frameSize});

  final ui.Image poster;
  final Size posterSize;
  final Map<String, ui.Image> sprites;
  final List<double> inv;
  final Size frameSize;

  /// The sprite of `v`; the resting mouth when that one is missing, or null when there is none at all.
  ui.Image? spriteFor(String v) => sprites[v] ?? sprites['sil'];

  static Future<ui.Image> decode(Uint8List bytes) async {
    final codec = await ui.instantiateImageCodec(bytes);
    final frame = await codec.getNextFrame();
    return frame.image;
  }

  /// Reads and decodes what `store` holds for `name`; null when the photo, the frame or the resting sprite is not usable. A sprite that
  /// does not decode is left out, and the resting one stands in for it.
  static Future<VisemePack?> load(AvatarAssetStore store, String name, {Future<ui.Image> Function(Uint8List)? decoder}) async {
    final decodeImage = decoder ?? decode;
    try {
      final frame = jsonDecode(await File(store.pathOf(name, 'visemes/frame.json')).readAsString());
      if (frame is! Map || frame['size'] is! List || frame['inv'] is! List) return null;
      final size = [for (final v in frame['size'] as List) (v as num).toDouble()];
      final inv = [for (final v in frame['inv'] as List) (v as num).toDouble()];
      if (size.length != 2 || inv.length != 6) return null;
      final poster = await decodeImage(await File(store.pathOf(name, 'poster.jpg')).readAsBytes());
      final sprites = <String, ui.Image>{};
      for (final v in visemeNames) {
        try {
          sprites[v] = await decodeImage(await File(store.pathOf(name, 'visemes/$v.png')).readAsBytes());
        } catch (_) {
          // the resting sprite stands in for it
        }
      }
      if (!sprites.containsKey('sil')) return null;
      final pw = (frame['poster_w'] as num?)?.toDouble() ?? poster.width.toDouble();
      final ph = (frame['poster_h'] as num?)?.toDouble() ?? poster.height.toDouble();
      return VisemePack(poster: poster, posterSize: Size(pw, ph), sprites: sprites, inv: inv, frameSize: Size(size[0], size[1]));
    } catch (_) {
      return null;
    }
  }
}

/// The mouth drawn from the PC's timeline, on the clock of the voice being played.
class VisemeRenderer extends AvatarRenderer {
  VisemeRenderer({required this.pack, required this.player, VisemeTimeline? timeline, int Function()? now})
      : timeline = timeline ?? VisemeTimeline(),
        _now = now ?? _wall;

  static final Stopwatch _clock = Stopwatch()..start();
  static int _wall() => _clock.elapsedMilliseconds;

  final VisemePack pack;
  final PcmPlayer player;
  final VisemeTimeline timeline;
  final int Function() _now;
  int _rate = 24000;

  VisemeFrame frame = VisemeFrame.rest;
  (double, double, double, double) motion = (0, 0, 0, 1);

  @override
  String get mode => 'visemes';

  @override
  void audioStart(int sampleRate) => _rate = sampleRate;

  @override
  void visemes(Map<String, dynamic> message) {
    final s = VisemeSegment.fromMessage(message, player.queuedMs, sampleRate: _rate);
    if (s != null) timeline.add(s);
  }

  @override
  void flush() {
    timeline.flush();
    frame = VisemeFrame.rest;
    notifyListeners();
  }

  @override
  void tick() {
    frame = timeline.at(player.playedMs);
    motion = IdleMotion(reduced: reducedMotion).at(_now());
    notifyListeners();
  }

  @override
  FaceScene? get scene {
    final layers = <FaceLayer>[];
    final prev = pack.spriteFor(frame.prev);
    final cur = pack.spriteFor(frame.v);
    if (prev != null && frame.mix < 1 && frame.prev != frame.v) layers.add(FaceLayer(prev, pack.inv, pack.frameSize));
    if (cur != null) layers.add(FaceLayer(cur, pack.inv, pack.frameSize, opacity: frame.prev == frame.v ? 1 : frame.mix));
    return FaceScene(poster: pack.poster, posterSize: pack.posterSize, motion: motion, layers: layers);
  }
}

/// Draws a [FaceScene] fitted into the space it is given.
class FacePainter extends CustomPainter {
  FacePainter(this.renderer) : super(repaint: renderer);
  final AvatarRenderer renderer;

  @override
  void paint(Canvas canvas, Size size) {
    final scene = renderer.scene;
    if (scene == null) return;
    final ps = scene.posterSize;
    final fit = math.min(size.width / ps.width, size.height / ps.height);
    if (fit <= 0 || fit.isNaN) return;
    canvas.save();
    canvas.translate((size.width - ps.width * fit) / 2, (size.height - ps.height * fit) / 2);
    canvas.scale(fit);
    canvas.clipRect(Offset.zero & ps);
    final (dx, dy, angle, scale) = scene.motion;
    canvas.translate(ps.width / 2 + dx * ps.width, ps.height / 2 + dy * ps.height);
    canvas.rotate(angle);
    canvas.scale(scale);
    canvas.translate(-ps.width / 2, -ps.height / 2);
    final paint = Paint()..filterQuality = FilterQuality.medium;
    canvas.drawImageRect(scene.poster, Rect.fromLTWH(0, 0, scene.poster.width.toDouble(), scene.poster.height.toDouble()), Offset.zero & ps, paint);
    for (final layer in scene.layers) {
      final m = layer.inv;
      canvas.save();
      canvas.transform(Float64List.fromList([m[0], m[3], 0, 0, m[1], m[4], 0, 0, 0, 0, 1, 0, m[2], m[5], 0, 1]));
      final dst = Offset.zero & layer.size;
      final src = Rect.fromLTWH(0, 0, layer.image.width.toDouble(), layer.image.height.toDouble());
      final p = Paint()
        ..filterQuality = FilterQuality.medium
        ..color = Color.fromRGBO(255, 255, 255, layer.opacity.clamp(0.0, 1.0));
      final clip = layer.clip;
      if (clip == null) {
        canvas.drawImageRect(layer.image, src, dst, p);
      } else {
        canvas.saveLayer(clip.inflate(1), Paint());
        canvas.drawImageRect(layer.image, src, dst, p);
        final feather = Paint()
          ..blendMode = BlendMode.dstIn
          ..shader = ui.Gradient.radial(clip.center, math.max(clip.width, clip.height) / 2, const [Color(0xFFFFFFFF), Color(0xFFFFFFFF), Color(0x00FFFFFF)], const [0, 0.7, 1]);
        canvas.drawRect(clip.inflate(1), feather);
        canvas.restore();
      }
      canvas.restore();
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(FacePainter old) => old.renderer != renderer;
}

/// The renderer's picture in the tab. It steps the renderer about 30 times a second while it is on the screen, and tells it when the phone
/// asks for reduced motion.
class AvatarFaceView extends StatefulWidget {
  const AvatarFaceView({super.key, required this.renderer});
  final AvatarRenderer renderer;

  @override
  State<AvatarFaceView> createState() => _AvatarFaceViewState();
}

class _AvatarFaceViewState extends State<AvatarFaceView> with SingleTickerProviderStateMixin {
  late final Ticker _ticker;
  Duration _last = Duration.zero;

  @override
  void initState() {
    super.initState();
    _ticker = createTicker((elapsed) {
      if (elapsed - _last < const Duration(milliseconds: 30)) return;
      _last = elapsed;
      widget.renderer.tick();
    })..start();
  }

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    widget.renderer.reducedMotion = MediaQuery.disableAnimationsOf(context);
    return RepaintBoundary(child: CustomPaint(painter: FacePainter(widget.renderer), size: Size.infinite));
  }
}
