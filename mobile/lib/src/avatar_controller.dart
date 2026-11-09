import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';
import 'audio_route.dart';
import 'avatar_assets.dart';
import 'avatar_devices.dart';
import 'avatar_engines.dart';
import 'avatar_hub.dart';
import 'avatar_renderer.dart';
import 'barge_in.dart';
import 'gateway_client.dart';
import 'i18n.dart';
import 'sdp.dart';
import 'settings.dart';
import 'wav2lip_engine.dart';

/// Builds the renderer of `mode` ('visemes' or 'wav2lip') for the avatar folder `name`, or null when it cannot (`why` then says why).
typedef RendererFactory = Future<AvatarRenderer?> Function(String mode, String name, void Function(String why) why);

/// The Avatar tab: it finds the voice gateway through the server, keeps the connection, plays the PC's voice, sends the microphone while the
/// button is held, and shows the PC's video when the avatar function is loaded there. Without it the tab works by voice only, and it
/// switches to the video by itself when the avatar starts. While the avatar speaks and the audio route allows full duplex, a local voice
/// detector watches the microphone: the avatar's voice is lowered at once and the PC decides what the interruption means.
class AvatarController extends ChangeNotifier {
  AvatarController({
    required this.api,
    required this.settings,
    required this.devices,
    GatewayClient? client,
    this.retry = const Duration(seconds: 5),
    this.videoWait = const Duration(seconds: 40),
    this.duckHold = const Duration(seconds: 6),
    EngineHub? hub,
    RendererFactory? rendererFactory,
  })  : client = client ?? GatewayClient(connector: connectSocket),
        hub = hub ?? EngineHub(settings: settings),
        _factory = rendererFactory,
        route = AudioRouteController(router: devices.router, settings: settings) {
    _seenEnabled = settings.avatarEnabled;
    _seenBrain = settings.avatarBrain;
    _seenPersona = settings.avatarPersona;
    _seenMode = mode;
    settings.addListener(_onSettings);
    this.hub.addListener(_replan);
    this.client
      ..addListener(_onClient)
      ..authFields = (() => (_declared = this.hub.plan()).fields)
      ..onAudioStart = ((rate) {
        if (!this.client.barging) _setGain(1);
        devices.player.begin(rate);
        renderer?.audioStart(rate);
      })
      ..onAudio = ((pcm) {
        devices.player.add(pcm);
        renderer?.audio(pcm);
      })
      ..onAudioEnd = (() {
        devices.player.finish();
        renderer?.audioEnd();
      })
      ..onInterrupt = (() {
        devices.player.cancel();
        renderer?.flush();
      })
      ..onVisemes = ((m) => renderer?.visemes(m))
      ..onReplyReset = (() => renderer?.flush())
      ..onRender = _onRender
      ..onDuck = _setGain
      ..onReady = _onReady
      ..onDropped = _onDropped
      ..onAvatar = ((_) => _maybeVideo())
      ..onVideoAnswer = _onAnswer
      ..onVideoUnavailable = ((_) => _dropVideo(tell: false))
      ..onVideoStopped = (() => _dropVideo(tell: false));
  }

  final Api api;
  final AppSettings settings;
  final AvatarDevices devices;
  final GatewayClient client;
  final AudioRouteController route;

  /// What this phone can draw on its own (the avatar's files, the Wav2Lip model and its self-test) and what it declares to the PC.
  final EngineHub hub;
  final RendererFactory? _factory;
  final Duration retry;
  final Duration videoWait;

  /// How long the voice stays lowered after the person stops speaking over it when the PC has not decided.
  final Duration duckHold;

  bool active = false;
  bool micDenied = false;
  String? unavailable;
  List<Map<String, String>> personas = const [];
  VideoSession? _video;

  /// The avatar drawn on this phone (null: the PC's video, or voice only) and a text for why it stopped, when it did.
  AvatarRenderer? renderer;
  String rendererNote = '';
  RenderPlan? _declared;
  String _renderKey = '';
  int _renderRun = 0;
  Timer? _videoTimer;
  Timer? _retryTimer;
  Timer? _duckTimer;
  StreamSubscription<Uint8List>? _micSub;
  StreamSubscription<Uint8List>? _bargeSub;
  BargeIn? _vad;
  bool _bargeMic = false;
  bool _holding = false;
  late bool _seenEnabled;
  late String _seenBrain;
  late String _seenPersona;
  late String _seenMode;

  String get mode => selectableEngines.contains(settings.avatarMode) ? settings.avatarMode : 'video';

  /// False: voice only, no picture asked from the PC.
  bool get avatarOn => settings.avatarEnabled;

  /// Whether this phone can draw the avatar in `m` now.
  bool modeAvailable(String m) => m == 'video' ? devices.videoSupported : (m == 'visemes' ? hub.visemesReady : (m == 'wav2lip' && hub.wav2lipReady));

  /// The picture of the avatar, while the PC's stream is live.
  VideoSession? get liveVideo => (_video != null && client.video == VideoStage.live) ? _video : null;

  bool get videoRequested => _video != null;

  String get lang {
    final l = settings.speechLocale;
    return l.isNotEmpty ? l.split(RegExp('[-_]')).first : Texts.language;
  }

  // ---- the tab is shown or hidden

  Future<void> activate() async {
    if (active) return;
    active = true;
    notifyListeners();
    await route.start();
    await hub.refresh();
    await _connect();
  }

  Future<void> deactivate() async {
    if (!active) return;
    active = false;
    _retryTimer?.cancel();
    _dropRenderer();
    await holdEnd(send: false);
    await _bargeStop();
    await _dropVideo(tell: true);
    client.close();
    await devices.player.cancel();
    _setGain(1);
    await route.stop();
    notifyListeners();
  }

  Future<void> _connect() async {
    _retryTimer?.cancel();
    if (!active) return;
    if (!settings.configured) {
      unavailable = Texts.t('avatar.setup');
      notifyListeners();
      return;
    }
    try {
      final info = await api.get('/api/voice/gateway');
      if (!active) return;
      personas = [for (final p in (info['personas'] as List? ?? const []).whereType<Map>()) {'slug': '${p['slug']}', 'name': '${p['name']}'}];
      if (info['available'] == true) {
        unavailable = null;
        final base = settings.base!;
        final url = base.replace(scheme: base.scheme == 'https' ? 'wss' : 'ws', path: '${info['path'] ?? '/api/voice/gateway/ws'}');
        client.open(url, api.headers);
        notifyListeners();
        return;
      }
      unavailable = '${info['reason'] ?? Texts.t('avatar.down')}';
    } on ApiError catch (e) {
      unavailable = e.message;
    }
    notifyListeners();
    _retryTimer = Timer(retry, _connect);
  }

  void _onClient() {
    if (client.video == VideoStage.live) _videoTimer?.cancel();
    _syncBarge();
    notifyListeners();
  }

  /// A setting changed, here or in the Settings screen: what the PC must know is sent once.
  void _onSettings() {
    final ready = client.status == GatewayStatus.ready;
    if (settings.avatarEnabled != _seenEnabled) {
      _seenEnabled = settings.avatarEnabled;
      if (_seenEnabled) {
        _maybeVideo();
      } else {
        _dropVideo(tell: false);
        if (ready) client.sendVideoStop();
      }
    }
    if (settings.avatarBrain != _seenBrain) {
      _seenBrain = settings.avatarBrain;
      if (ready && _seenBrain.isNotEmpty) client.setBrain(_seenBrain);
    }
    if (settings.avatarPersona != _seenPersona) {
      _seenPersona = settings.avatarPersona;
      if (ready) client.setPersona(_seenPersona);
    }
    if (mode != _seenMode) {
      _seenMode = mode;
      _applyMode();
    }
    _replan();
    _syncRenderer();
    notifyListeners();
  }

  /// What the app can draw has changed (a download, a test, the preference): when it is not what the PC was told, the PC is told again,
  /// which can only be done by opening the connection again.
  void _replan() {
    final d = _declared;
    if (d == null || !active || client.status != GatewayStatus.ready) return;
    if (!hub.plan().sameAs(d)) client.reconnect();
  }

  // ---- the avatar drawn here

  /// The PC named a mode and an avatar folder (`ready`, `render_mode`): remember the folder, fetch its files when the preference needs
  /// them, and show the renderer of the mode when it is one of ours.
  void _onRender() {
    final name = client.avatarName;
    if (name.isNotEmpty && name != settings.avatarName) settings.setAvatarName(name);
    _fetchAssets(name);
    _syncRenderer();
    notifyListeners();
  }

  Future<void> _fetchAssets(String name) async {
    final store = hub.store;
    if (name.isEmpty || !avatarOn || store == null || !(mode == 'visemes' || mode == 'wav2lip')) return;
    final stage = store.stageOf(name);
    if (stage == AssetStage.ready || stage == AssetStage.downloading) return;
    await hub.fetchAssets(name);
    if (active) {
      _replan();
      _syncRenderer();
    }
  }

  Future<void> _syncRenderer() async {
    final m = client.renderMode;
    final name = client.avatarName;
    final want = (active && avatarOn && (m == 'visemes' || m == 'wav2lip') && name.isNotEmpty) ? m : '';
    final key = want.isEmpty ? '' : '$want|$name';
    if (key == _renderKey) return;
    _renderKey = key;
    final run = ++_renderRun;
    _dropRenderer(keepKey: true);
    rendererNote = '';
    if (key.isEmpty) {
      notifyListeners();
      return;
    }
    var why = 'assets_missing';
    AvatarRenderer? r;
    try {
      r = await (_factory ?? _build)(want, name, (w) => why = w);
    } catch (_) {
      why = 'inference_failed';
    }
    if (run != _renderRun || !active) {
      r?.dispose();
      return;
    }
    if (r == null) {
      _failed(want, why);
      return;
    }
    r.onFailed = (w) => _failed(want, w);
    renderer = r;
    notifyListeners();
  }

  /// The built-in renderers: the files from the store (fetched when they are not there yet) and, for Wav2Lip, the model.
  Future<AvatarRenderer?> _build(String mode, String name, void Function(String) why) async {
    final store = hub.store;
    if (store == null) return null;
    if (store.stageOf(name) != AssetStage.ready && !await hub.fetchAssets(name)) return null;
    if (mode == 'visemes') {
      final pack = await VisemePack.load(store, name);
      if (pack == null) {
        why('assets_invalid');
        return null;
      }
      return VisemeRenderer(pack: pack, player: devices.player);
    }
    final backend = hub.backend;
    if (backend == null || !hub.wav2lipReady) {
      why(backend == null ? 'no_runtime' : 'not_ready');
      return null;
    }
    final pack = await Wav2LipPack.load(store, name);
    if (pack == null) {
      why('assets_invalid');
      return null;
    }
    final loaded = await backend.load(model: store.modelFile.path, face: pack.facePath);
    if (!loaded.ok) {
      why(loaded.reason.isEmpty ? 'onnx_load_failed' : loaded.reason);
      return null;
    }
    return Wav2LipRenderer(backend: backend, pack: pack, player: devices.player);
  }

  /// The mode in use does not work on this phone: it is left out from now on and the PC is told, which answers with the next one.
  void _failed(String mode, String why) {
    _dropRenderer(keepKey: true);
    // The PC leaves the mode out for the rest of this connection, so what it was told stays true: only later connections change.
    client.sendRenderFailed(mode, why);
    _declared = null;
    hub.suppress(mode);
    _declared = hub.plan();
    rendererNote = Texts.t('avatar.render.failed', [mode, why]);
    notifyListeners();
  }

  void _dropRenderer({bool keepKey = false}) {
    final r = renderer;
    renderer = null;
    if (!keepKey) {
      _renderKey = '';
      _renderRun++;
    }
    r?.dispose();
  }

  // ---- the connection

  void _onReady() {
    _onRender();
    if (settings.avatarBrain.isNotEmpty && settings.avatarBrain != client.brain) client.setBrain(settings.avatarBrain);
    if (settings.avatarPersona.isNotEmpty && settings.avatarPersona != client.persona) client.setPersona(settings.avatarPersona);
    if (mode == 'video') _chooseVideo();
    if (!avatarOn) client.sendVideoStop();
    _maybeVideo();
  }

  /// Asks the PC for the video only when it offers it and is not already using it; the voice never depends on this.
  void _chooseVideo() {
    if (client.status != GatewayStatus.ready) return;
    if (client.renderMode == 'video' || !client.renderModes.contains('video')) return;
    client.setRenderMode('video');
  }

  void _onDropped() {
    _dropRenderer();
    _dropVideo(tell: false);
    _holding = false;
    _micSub?.cancel();
    _bargeStop();
    devices.mic.stop();
    devices.player.cancel();
    _setGain(1);
  }

  // ---- the video

  Future<void> _maybeVideo() async {
    if (!active || !avatarOn || _video != null || !(mode == 'video' || client.renderMode == 'video') || !client.avatar || client.status != GatewayStatus.ready || !devices.videoSupported) return;
    final session = devices.newVideo();
    if (session == null) return;
    _video = session;
    session.onConnection = (up) => notifyListeners();
    try {
      final offer = preferH264(await session.createOffer());
      if (_video != session) return;
      final quality = settings.avatarQuality;
      client.sendVideoStart(offer, resolution: settings.avatarResolution, fps: quality.fps, bitrateKbps: quality.bitrateKbps);
      _videoTimer?.cancel();
      _videoTimer = Timer(videoWait, () {
        if (_video == session && client.video != VideoStage.live) {
          _dropVideo(tell: true);
        }
      });
    } catch (e) {
      client.problem = '$e';
      client.problemCode = 'video';
      await _dropVideo(tell: false);
    }
    notifyListeners();
  }

  Future<void> _onAnswer(String sdp) async {
    final session = _video;
    if (session == null) return;
    try {
      await session.setAnswer(sdp);
    } catch (e) {
      client.problem = '$e';
      client.problemCode = 'video';
      await _dropVideo(tell: true);
    }
  }

  /// Closes the video session; `tell` also asks the PC to stop sending.
  Future<void> _dropVideo({required bool tell}) async {
    _videoTimer?.cancel();
    final session = _video;
    _video = null;
    if (session != null) {
      if (tell) client.sendVideoStop();
      try {
        await session.close();
      } catch (_) {}
    } else if (tell && client.video != VideoStage.off) {
      client.sendVideoStop();
    }
    notifyListeners();
  }

  Future<void> setMode(String m) async {
    if (!selectableEngines.contains(m)) return;
    _seenMode = m;
    await settings.setAvatarMode(m);
    await _applyMode();
    notifyListeners();
  }

  Future<void> _applyMode() async {
    if (mode == 'video') {
      _chooseVideo();
      await _maybeVideo();
    } else if (client.renderMode != 'video') {
      await _dropVideo(tell: true);
    }
  }

  /// The tab's quick switch (the same setting as in Settings): off is voice only, and the PC is told to stop the video.
  Future<void> setAvatarEnabled(bool on) => settings.setAvatarEnabled(on);

  // ---- talking

  /// The button went down.
  Future<void> holdStart() async {
    if (_holding || !client.canTalk) return;
    _holding = true;
    await _bargeStop();
    micDenied = false;
    final stream = await devices.mic.start();
    if (!_holding) {
      await devices.mic.stop();
      return;
    }
    if (stream == null) {
      _holding = false;
      micDenied = true;
      notifyListeners();
      return;
    }
    if (!client.holdStart(lang)) {
      _holding = false;
      await devices.mic.stop();
      return;
    }
    _micSub = stream.listen(client.sendAudio);
  }

  /// The button was released; `send: false` drops the turn without asking the PC to answer.
  Future<void> holdEnd({bool send = true}) async {
    if (!_holding) return;
    _holding = false;
    await _micSub?.cancel();
    _micSub = null;
    await devices.mic.stop();
    if (send) client.holdEnd();
  }

  /// For a person who cannot hold the button: one tap starts, the next sends.
  Future<void> toggleTalk() => _holding ? holdEnd() : holdStart();

  bool get holding => _holding;

  bool sendText(String text) => client.sendText(text, lang);

  void stopSpeaking() => client.interrupt();

  Future<void> setBrain(String value) async {
    _seenBrain = value;
    await settings.setAvatarBrain(value);
    client.setBrain(value);
  }

  Future<void> setPersona(String slug) async {
    _seenPersona = slug;
    await settings.setAvatarPersona(slug);
    client.setPersona(slug);
  }

  /// The person took (or turned down) the continuation the PC offered after an interruption.
  void answerResume(bool accept) => client.answerResume(accept);

  // ---- the person speaks over the avatar

  void _setGain(double gain) {
    _duckTimer?.cancel();
    devices.player.setGain(gain);
  }

  /// While the avatar speaks and the route allows full duplex the microphone stays open for a local voice detector; otherwise it is gated:
  /// nothing is captured, and the hold button is the way to interrupt.
  void _syncBarge() {
    final want = active && !_holding && route.fullDuplex && client.canTalk && client.phase == Phase.speaking;
    if (want) {
      _bargeListen();
    } else if (_bargeMic && !client.barging) {
      _bargeStop();
    }
  }

  Future<void> _bargeListen() async {
    if (_bargeMic) return;
    _bargeMic = true;
    final stream = await devices.mic.start();
    if (!_bargeMic) {
      await devices.mic.stop();
      return;
    }
    if (stream == null) {
      _bargeMic = false;
      micDenied = true;
      notifyListeners();
      return;
    }
    final vad = BargeIn(onStart: _bargeBegin, onEnd: _bargeFinish);
    _vad = vad;
    _bargeSub = stream.listen((pcm) {
      final was = vad.speaking;
      vad.feed(pcm);
      if (was && client.barging) client.sendAudio(pcm);
    });
  }

  void _bargeBegin(double level, List<Uint8List> preroll) {
    if (!client.bargeStart(lang, level)) return;
    _setGain(duckLevel);
    for (final chunk in preroll) {
      client.sendAudio(chunk);
    }
  }

  void _bargeFinish(double level, int ms) {
    if (!client.barging) return;
    client.bargeEnd(level, ms);
    _duckTimer?.cancel();
    _duckTimer = Timer(duckHold, () => devices.player.setGain(1));
  }

  Future<void> _bargeStop() async {
    if (!_bargeMic) return;
    _bargeMic = false;
    await _bargeSub?.cancel();
    _bargeSub = null;
    _vad?.finish();
    _vad = null;
    await devices.mic.stop();
  }

  bool _disposed = false;

  @override
  void notifyListeners() {
    if (!_disposed) super.notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _retryTimer?.cancel();
    _videoTimer?.cancel();
    _duckTimer?.cancel();
    settings.removeListener(_onSettings);
    hub.removeListener(_replan);
    _dropRenderer();
    client.removeListener(_onClient);
    client.close();
    route.dispose();
    super.dispose();
  }
}
