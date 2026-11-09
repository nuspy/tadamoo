import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';
import 'audio_route.dart';
import 'avatar_devices.dart';
import 'avatar_engines.dart';
import 'barge_in.dart';
import 'gateway_client.dart';
import 'i18n.dart';
import 'sdp.dart';
import 'settings.dart';

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
  })  : client = client ?? GatewayClient(connector: connectSocket),
        route = AudioRouteController(router: devices.router, settings: settings) {
    _seenEnabled = settings.avatarEnabled;
    _seenBrain = settings.avatarBrain;
    _seenPersona = settings.avatarPersona;
    _seenMode = mode;
    settings.addListener(_onSettings);
    this.client
      ..addListener(_onClient)
      ..onAudioStart = ((rate) {
        if (!this.client.barging) _setGain(1);
        devices.player.begin(rate);
      })
      ..onAudio = devices.player.add
      ..onAudioEnd = (() => devices.player.finish())
      ..onInterrupt = (() => devices.player.cancel())
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
  final Duration retry;
  final Duration videoWait;

  /// How long the voice stays lowered after the person stops speaking over it when the PC has not decided.
  final Duration duckHold;

  bool active = false;
  bool micDenied = false;
  String? unavailable;
  List<Map<String, String>> personas = const [];
  VideoSession? _video;
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

  /// Whether this build can draw the avatar in `m`.
  bool modeAvailable(String m) => m == 'video' && devices.videoSupported;

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
    await _connect();
  }

  Future<void> deactivate() async {
    if (!active) return;
    active = false;
    _retryTimer?.cancel();
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
    notifyListeners();
  }

  // ---- the connection

  void _onReady() {
    if (settings.avatarBrain.isNotEmpty && settings.avatarBrain != client.brain) client.setBrain(settings.avatarBrain);
    if (settings.avatarPersona.isNotEmpty && settings.avatarPersona != client.persona) client.setPersona(settings.avatarPersona);
    if (mode == 'video') client.setRenderMode('musetalk');
    if (!avatarOn) client.sendVideoStop();
    _maybeVideo();
  }

  void _onDropped() {
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
    if (!active || !avatarOn || _video != null || mode != 'video' || !client.avatar || client.status != GatewayStatus.ready || !devices.videoSupported) return;
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
      client.setRenderMode('musetalk');
      await _maybeVideo();
    } else {
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
    client.removeListener(_onClient);
    client.close();
    route.dispose();
    super.dispose();
  }
}
