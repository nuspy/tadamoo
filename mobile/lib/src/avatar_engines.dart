/// One way the avatar can be drawn. The chooser in Settings is built from `avatarEngines`: a new engine is one entry here plus its runtime.
class AvatarEngine {
  const AvatarEngine({
    required this.id,
    required this.nameKey,
    required this.weightKey,
    required this.noteKey,
    this.selectable = false,
    this.hasRuntime = false,
    this.pcId = '',
    this.mode = '',
    this.heights = const [],
  });

  /// The value saved in the settings.
  final String id;
  final String nameKey;

  /// Text keys: the size on the phone and a line on speed and quality.
  final String weightKey;
  final String noteKey;

  /// Whether the person may choose it today; the others are listed but disabled.
  final bool selectable;

  /// Whether this build can actually draw with it.
  final bool hasRuntime;

  /// The engine's id on the PC (`engines.md`) and the render mode it is served as ('' for none); `heights` are the output heights it offers.
  final String pcId;
  final String mode;
  final List<int> heights;
}

const avatarEngines = <AvatarEngine>[
  AvatarEngine(
      id: 'visemes', nameKey: 'eng.visemes', weightKey: 'eng.visemes.w', noteKey: 'eng.visemes.n', selectable: true, hasRuntime: true, pcId: 'visemes', mode: 'visemes', heights: [96, 144, 192, 240, 288, 384, 480]),
  AvatarEngine(
      id: 'wav2lip', nameKey: 'eng.wav2lip', weightKey: 'eng.wav2lip.w', noteKey: 'eng.wav2lip.n', selectable: true, hasRuntime: true, pcId: 'wav2lip_gan96', mode: 'wav2lip', heights: [96, 144, 192, 288, 384, 480]),
  AvatarEngine(id: 'ultralight', nameKey: 'eng.ultralight', weightKey: 'eng.ultralight.w', noteKey: 'eng.ultralight.n', selectable: true, hasRuntime: true, mode: 'ultralight'),
  AvatarEngine(id: 'feathertalk', nameKey: 'eng.feathertalk', weightKey: 'eng.feathertalk.w', noteKey: 'eng.feathertalk.n', selectable: true, hasRuntime: true, mode: 'feathertalk'),
  AvatarEngine(id: 'video', nameKey: 'eng.video', weightKey: 'eng.video.w', noteKey: 'eng.video.n', selectable: true, hasRuntime: true, mode: 'video'),
];

/// The engines the person may choose today.
List<String> get selectableEngines => [for (final e in avatarEngines) if (e.selectable) e.id];

/// The picture sizes offered, in pixels (the longer side). A bigger picture is not always better: it costs time and battery.
const avatarResolutions = [96, 144, 192, 240, 288, 384, 480];
const defaultAvatarResolution = 240;

/// How much of the connection and the processor the picture may use.
enum AvatarQuality {
  low(15, 300),
  medium(25, 800),
  high(30, 1800);

  const AvatarQuality(this.fps, this.bitrateKbps);
  final int fps;
  final int bitrateKbps;

  static AvatarQuality fromName(String name) => AvatarQuality.values.firstWhere((q) => q.name == name, orElse: () => AvatarQuality.medium);
}
