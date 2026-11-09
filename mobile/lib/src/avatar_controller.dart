import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api.dart';
import 'avatar_devices.dart';
import 'gateway_client.dart';
import 'i18n.dart';
import 'sdp.dart';
import 'settings.dart';

/// How the avatar can be drawn. Only 'video' (the PC's picture over WebRTC) has a runtime in this build.
const avatarModes = ['video', 'wav2lip', 'visemes'];

/// The Avatar tab: it finds the voice gateway through the server, keeps the connection, plays the PC's voice, sends the microphone while the
/// button is held, and shows the PC's video when the avatar function is loaded there. Without it the tab works by voice only, and it
/// switches to the video by itself when the avatar starts.
class AvatarController extends ChangeNotifier {
  AvatarController({
    required this.api,
    required this.settings,
    required this.devices,
    GatewayClient? client,
    this.retry = const Duration(seconds: 5),
    this.videoWait = const Duration(seconds: 40),
  }) : client = client ?? GatewayClient(connector: connectSocket) {
    this.client
      ..addListener(_onClient)
      ..onAudioStart = ((rate) => devices.player.begin(rate))
      ..onAudio = devices.player.add
      ..onAudioEnd = (() => devices.player.finish())
      ..onInterrupt = (() => devices.player.cancel())
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
  final Duration retry;
  final Duration videoWait;

  bool active = false;
  bool micDenied = false;
  String? unavailable;
  List<Map<String, String>> personas = const [];
  VideoSession? _video;
  Timer? _videoTimer;
  Timer? _retryTimer;
  StreamSubscription<Uint8List>? _micSub;
  bool _holding = false;

  String get mode => avatarModes.contains(settings.avatarMode) ? settings.avatarMode : 'video';

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
    await _connect();
  }

  Future<void> deactivate() async {
    if (!active) return;
    active = false;
    _retryTimer?.cancel();
    await holdEnd(send: false);
    await _dropVideo(tell: true);
    client.close();
    await devices.player.cancel();
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
    notifyListeners();
  }

  // ---- the connection

  void _onReady() {
    if (settings.avatarBrain.isNotEmpty && settings.avatarBrain != client.brain) client.setBrain(settings.avatarBrain);
    if (settings.avatarPersona.isNotEmpty && settings.avatarPersona != client.persona) client.setPersona(settings.avatarPersona);
    if (mode == 'video') client.setRenderMode('musetalk');
    _maybeVideo();
  }

  void _onDropped() {
    _dropVideo(tell: false);
    _holding = false;
    _micSub?.cancel();
    devices.mic.stop();
    devices.player.cancel();
  }

  // ---- the video

  Future<void> _maybeVideo() async {
    if (!active || _video != null || mode != 'video' || !client.avatar || client.status != GatewayStatus.ready || !devices.videoSupported) return;
    final session = devices.newVideo();
    if (session == null) return;
    _video = session;
    session.onConnection = (up) => notifyListeners();
    try {
      final offer = preferH264(await session.createOffer());
      if (_video != session) return;
      client.sendVideoStart(offer);
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
    if (!avatarModes.contains(m)) return;
    await settings.setAvatarMode(m);
    if (m == 'video') {
      client.setRenderMode('musetalk');
      await _maybeVideo();
    } else {
      await _dropVideo(tell: true);
    }
    notifyListeners();
  }

  // ---- talking

  /// The button went down.
  Future<void> holdStart() async {
    if (_holding || !client.canTalk) return;
    _holding = true;
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
    await settings.setAvatarBrain(value);
    client.setBrain(value);
  }

  Future<void> setPersona(String slug) async {
    await settings.setAvatarPersona(slug);
    client.setPersona(slug);
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
    client.removeListener(_onClient);
    client.close();
    super.dispose();
  }
}
