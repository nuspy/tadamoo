import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';

/// One WebSocket to the PC's voice gateway (through the server, with this phone's key). Text frames are JSON, binary frames are 16-bit PCM.
abstract class GatewayChannel {
  Stream<dynamic> get messages;
  void sendText(String text);
  void sendBytes(List<int> bytes);
  Future<void> close();
}

typedef GatewayConnector = Future<GatewayChannel> Function(Uri url, Map<String, String> headers);

class _SocketChannel implements GatewayChannel {
  _SocketChannel(this._ws);
  final WebSocket _ws;
  @override
  Stream<dynamic> get messages => _ws;
  @override
  void sendText(String text) => _ws.add(text);
  @override
  void sendBytes(List<int> bytes) => _ws.add(bytes);
  @override
  Future<void> close() => _ws.close();
}

Future<GatewayChannel> connectSocket(Uri url, Map<String, String> headers) async {
  final ws = await WebSocket.connect(url.toString(), headers: headers).timeout(const Duration(seconds: 10));
  ws.pingInterval = const Duration(seconds: 20);
  return _SocketChannel(ws);
}

enum GatewayStatus { offline, connecting, ready }

enum Phase { idle, listening, thinking, speaking }

enum VideoStage { off, negotiating, preparing, live }

class Line {
  Line(this.mine, this.text);
  final bool mine;
  String text;
}

/// The protocol of the gateway (see voice/callbridge/voice_gateway.py) and the state of the conversation. It keeps the connection up by itself:
/// when it drops, it asks again after 1, 2, 4, 8 and then every 15 seconds, and it forgets the turn that was running.
class GatewayClient extends ChangeNotifier {
  GatewayClient({required this.connector, Future<void> Function(Duration)? wait}) : _wait = wait ?? ((d) => Future<void>.delayed(d));

  final GatewayConnector connector;
  final Future<void> Function(Duration) _wait;

  GatewayStatus status = GatewayStatus.offline;
  Phase phase = Phase.idle;
  VideoStage video = VideoStage.off;
  String videoDetail = '';
  String videoReason = '';
  bool voice = false;
  List<String> missing = const [];
  bool avatar = false;
  String brain = 'direct';
  String persona = '';
  /// What the PC serves to this device now (`ready.render_modes`, then `render_mode`), the mode in use ('' when not told, 'none' for voice
  /// only), the avatar's folder and, when the PC names them, the engine and the picture size.
  List<String> renderModes = const [];
  String renderMode = '';
  String avatarName = '';
  String engine = '';
  String resolution = '';
  String? problem;
  String problemCode = '';
  final List<Line> lines = [];

  /// The continuation the PC offers after an interruption ('' when none), and whether the person is speaking over the avatar now.
  String resumeOffer = '';
  bool barging = false;

  void Function(int sampleRate)? onAudioStart;
  void Function(Uint8List pcm)? onAudio;
  VoidCallback? onAudioEnd;
  VoidCallback? onInterrupt;
  VoidCallback? onReady;
  VoidCallback? onDropped;
  void Function(bool loaded)? onAvatar;
  void Function(String sdp)? onVideoAnswer;
  void Function(String reason)? onVideoUnavailable;
  VoidCallback? onVideoStopped;

  /// The modes this build can draw, best first, declared in the first message. Only the PC's video is drawn today: declaring more would make
  /// the PC stop sending the video.
  static const declaredModes = ['video'];
  static const deviceKind = 'android';

  /// The PC asks for the avatar's voice at this level (1 is normal, below is lowered).
  void Function(double level)? onDuck;

  Uri? _url;
  Map<String, String> _headers = const {};
  bool _wanted = false;
  int _attempt = 0;
  int _run = 0;
  int _pending = 0;
  bool _replyOpen = false;
  GatewayChannel? _channel;

  static const _backoff = [1, 2, 4, 8, 15];

  bool get canTalk => status == GatewayStatus.ready && voice;

  /// Keeps a connection to `url` for as long as `close` is not called.
  void open(Uri url, Map<String, String> headers) {
    _url = url;
    _headers = headers;
    if (_wanted) return;
    _wanted = true;
    _loop();
  }

  void close() {
    _wanted = false;
    _run++;
    final c = _channel;
    _channel = null;
    c?.close();
    _forget();
    notifyListeners();
  }

  Future<void> _loop() async {
    final run = ++_run;
    while (_wanted && run == _run) {
      status = GatewayStatus.connecting;
      notifyListeners();
      try {
        final channel = await connector(_url!, _headers);
        if (!_wanted || run != _run) {
          await channel.close();
          break;
        }
        _channel = channel;
        channel.sendText(jsonEncode({'type': 'auth', 'render_modes': declaredModes, 'device': deviceKind}));
        await for (final m in channel.messages) {
          if (run != _run) break;
          _onMessage(m);
        }
      } catch (e) {
        if (run != _run) break;
        problem = _reason(e);
        problemCode = e.toString().contains('401') ? 'unauthorized' : 'network';
      }
      if (run != _run) break;
      _channel = null;
      _forget();
      notifyListeners();
      final secs = _backoff[min(_attempt, _backoff.length - 1)];
      _attempt++;
      await _wait(Duration(seconds: secs));
    }
  }

  static String _reason(Object e) => e is SocketException ? e.message : '$e';

  /// A connection that is gone takes the turn, the video and what the PC could do with it.
  void _forget() {
    final wasReady = status == GatewayStatus.ready;
    status = GatewayStatus.offline;
    phase = Phase.idle;
    video = VideoStage.off;
    videoDetail = '';
    _pending = 0;
    _replyOpen = false;
    barging = false;
    resumeOffer = '';
    avatar = false;
    voice = false;
    renderModes = const [];
    renderMode = '';
    avatarName = '';
    engine = '';
    resolution = '';
    videoReason = '';
    if (wasReady) onDropped?.call();
  }

  void _send(Map<String, dynamic> message) => _channel?.sendText(jsonEncode(message));

  void _onMessage(dynamic m) {
    if (m is String) {
      Object? decoded;
      try {
        decoded = jsonDecode(m);
      } catch (_) {
        return;
      }
      if (decoded is Map) _onJson(Map<String, dynamic>.from(decoded));
    } else if (m is List<int>) {
      if (phase != Phase.listening) onAudio?.call(m is Uint8List ? m : Uint8List.fromList(m));
    }
  }

  List<String> _strings(Object? v) => v is List ? v.map((e) => '$e').toList() : const [];

  /// `ready` and `render_mode` carry the same fields; each one that is missing keeps what was known, and unknown ones are ignored.
  void _readRender(Map<String, dynamic> m) {
    if (m['render_modes'] is List) renderModes = _strings(m['render_modes']);
    if (m['render_mode'] != null) renderMode = '${m['render_mode']}';
    if (m['mode'] != null) renderMode = '${m['mode']}';
    if (m['avatar_name'] != null) avatarName = '${m['avatar_name']}';
    if (m['engine'] != null) engine = '${m['engine']}';
    if (m['resolution'] != null) resolution = '${m['resolution']}';
  }

  void _onJson(Map<String, dynamic> m) {
    switch (m['type']) {
      case 'ready':
        status = GatewayStatus.ready;
        _attempt = 0;
        problem = null;
        problemCode = '';
        voice = m['voice'] == true;
        missing = _strings(m['missing']);
        avatar = m['avatar'] == true;
        brain = '${m['brain'] ?? brain}';
        persona = '${m['persona'] ?? ''}';
        _readRender(m);
        videoReason = '';
        onReady?.call();
      case 'state':
        voice = m['voice'] == true;
        missing = _strings(m['missing']);
        if (voice && problemCode == 'not_loaded') {
          problem = null;
          problemCode = '';
        }
        final loaded = m['avatar'] == true;
        final changed = loaded != avatar;
        avatar = loaded;
        if (changed) onAvatar?.call(loaded);
      case 'render_mode':
        _readRender(m);
      case 'transcript':
        final text = '${m['text'] ?? ''}'.trim();
        if (text.isNotEmpty) {
          lines.add(Line(true, text));
          _replyOpen = false;
          if (phase != Phase.listening) phase = Phase.thinking;
        }
      case 'reply':
        final chunk = '${m['chunk'] ?? ''}';
        if (!_replyOpen) {
          lines.add(Line(false, ''));
          _replyOpen = true;
        }
        lines.last.text += chunk;
      case 'reply_reset':
        if (_replyOpen) lines.last.text = '${m['text'] ?? ''}';
      case 'audio_start':
        if (phase != Phase.listening) {
          phase = Phase.speaking;
          onAudioStart?.call((m['sample_rate'] as num?)?.toInt() ?? 24000);
        }
      case 'audio_end':
        if (phase != Phase.listening) onAudioEnd?.call();
      case 'done':
      case 'turn_done':
        if (_pending > 0) _pending--;
        _replyOpen = false;
        if (_pending == 0) phase = Phase.idle;
      case 'interrupt':
        _replyOpen = false;
        onInterrupt?.call();
      case 'duck':
        final level = m['level'];
        if (level is num) onDuck?.call(level.toDouble().clamp(0.0, 1.0));
      case 'resume_offer':
        resumeOffer = '${m['text'] ?? ''}'.trim();
      case 'brain':
        brain = '${m['brain'] ?? brain}';
      case 'persona':
        persona = '${m['persona'] ?? ''}';
      case 'video_answer':
        onVideoAnswer?.call('${m['sdp'] ?? ''}');
      case 'video_state':
        video = m['state'] == 'live' ? VideoStage.live : VideoStage.preparing;
        videoDetail = '${m['detail'] ?? ''}';
      case 'video_unavailable':
        video = VideoStage.off;
        videoReason = '${m['reason'] ?? ''}';
        onVideoUnavailable?.call(videoReason);
      case 'video_stopped':
        video = VideoStage.off;
        onVideoStopped?.call();
      case 'error':
        if (m['code'] == 'render_mode_unavailable') break;
        problem = '${m['message'] ?? ''}';
        problemCode = '${m['code'] ?? ''}';
    }
    notifyListeners();
  }

  // ---- what the person does

  /// The button went down: a new turn. Whatever the PC is saying stops (at once here, and on its side when it reads the start).
  bool holdStart(String lang) {
    if (!canTalk) return false;
    if (phase == Phase.speaking || phase == Phase.thinking) onInterrupt?.call();
    resumeOffer = '';
    phase = Phase.listening;
    _pending++;
    _send({'type': 'start', 'mode': 'hold', 'lang': lang, 'sample_rate': 16000});
    notifyListeners();
    return true;
  }

  void sendAudio(Uint8List pcm) {
    if (phase == Phase.listening || barging) _channel?.sendBytes(pcm);
  }

  /// The button was released: the PC transcribes and answers.
  void holdEnd() {
    if (phase != Phase.listening) return;
    phase = Phase.thinking;
    _send({'type': 'end'});
    notifyListeners();
  }

  bool sendText(String text, String lang) {
    final t = text.trim();
    if (t.isEmpty || !canTalk) return false;
    if (phase == Phase.speaking || phase == Phase.thinking) onInterrupt?.call();
    resumeOffer = '';
    lines.add(Line(true, t));
    _replyOpen = false;
    phase = Phase.thinking;
    _pending++;
    _send({'type': 'text', 'text': t, 'lang': lang});
    notifyListeners();
    return true;
  }

  /// Stops the reply that is being generated or played.
  void interrupt() {
    if (phase != Phase.speaking && phase != Phase.thinking) return;
    _send({'type': 'interrupt'});
    onInterrupt?.call();
  }

  /// The person began to speak over the avatar's voice: the PC is told, then the utterance follows as a turn that does not stop the reply
  /// by itself (`barge_in`); the PC answers with `duck`, `interrupt` or nothing.
  bool bargeStart(String lang, double level) {
    if (!canTalk || barging || phase != Phase.speaking) return false;
    barging = true;
    _pending++;
    resumeOffer = '';
    _send({'type': 'user_speaking', 'state': 'start', 'level': _round(level), 'ms': 0});
    _send({'type': 'start', 'mode': 'hold', 'lang': lang, 'sample_rate': 16000, 'barge_in': true});
    notifyListeners();
    return true;
  }

  /// The person stopped speaking over the avatar: `ms` is how long the speech lasted.
  void bargeEnd(double level, int ms) {
    if (!barging) return;
    barging = false;
    _send({'type': 'user_speaking', 'state': 'end', 'level': _round(level), 'ms': ms});
    _send({'type': 'end'});
    notifyListeners();
  }

  static double _round(double v) => (v * 1000).round() / 1000;

  /// The person took or turned down the continuation the PC offered.
  void answerResume(bool accept) {
    if (resumeOffer.isEmpty) return;
    resumeOffer = '';
    _send({'type': 'resume', 'accept': accept});
    notifyListeners();
  }

  void setBrain(String value) => _send({'type': 'brain', 'brain': value});

  void setPersona(String slug) => _send({'type': 'persona', 'persona': slug});

  void setRenderMode(String mode) => _send({'type': 'render_mode', 'mode': mode});

  /// `resolution` (pixels, longer side), `fps` and `bitrateKbps` are hints for the PC's video; the PC may keep its own.
  void sendVideoStart(String sdp, {String persona = '', int? resolution, int? fps, int? bitrateKbps}) {
    video = VideoStage.negotiating;
    videoDetail = '';
    _send({
      'type': 'video_start',
      'reason': 'emergency',
      'sdp': sdp,
      'sdp_type': 'offer',
      if (persona.isNotEmpty) 'persona': persona,
      'resolution': ?resolution,
      'fps': ?fps,
      'bitrate_kbps': ?bitrateKbps,
    });
    notifyListeners();
  }

  void sendVideoStop() {
    _send({'type': 'video_stop'});
    video = VideoStage.off;
    notifyListeners();
  }

  void clearProblem() {
    problem = null;
    problemCode = '';
    notifyListeners();
  }
}
