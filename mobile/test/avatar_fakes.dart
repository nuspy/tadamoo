import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:basalt_mobile/src/api.dart';
import 'package:basalt_mobile/src/audio_route.dart';
import 'package:basalt_mobile/src/avatar_devices.dart';
import 'package:basalt_mobile/src/gateway_client.dart';
import 'package:flutter/widgets.dart';

/// The PC's side of one WebSocket: what the phone sent is in `sent`; `say` and `bytes` are what the gateway sends.
class FakeChannel implements GatewayChannel {
  final StreamController<dynamic> _in = StreamController<dynamic>();
  final List<Map<String, dynamic>> sent = [];
  final List<Uint8List> sentBytes = [];
  bool closed = false;

  @override
  Stream<dynamic> get messages => _in.stream;

  @override
  void sendText(String text) => sent.add(Map<String, dynamic>.from(jsonDecode(text) as Map));

  @override
  void sendBytes(List<int> bytes) => sentBytes.add(Uint8List.fromList(bytes));

  @override
  Future<void> close() async {
    closed = true;
    if (!_in.isClosed) await _in.close();
  }

  void say(Map<String, dynamic> message) => _in.add(jsonEncode(message));
  void bytes(List<int> data) => _in.add(Uint8List.fromList(data));
  void drop() => _in.close();

  List<String> get kinds => sent.map((m) => '${m['type']}').toList();
  Map<String, dynamic>? last(String type) => sent.cast<Map<String, dynamic>?>().lastWhere((m) => m!['type'] == type, orElse: () => null);
}

const readyMessage = {'type': 'ready', 'voice': true, 'missing': [], 'avatar': false, 'brain': 'direct', 'persona': '', 'render_mode': 'video', 'render_modes': ['video'], 'avatar_name': 'ada'};

/// Hands out channels in order; the first `fail` attempts throw.
class FakeConnector {
  final List<FakeChannel> channels = [];
  final List<Uri> urls = [];
  final List<Map<String, String>> headers = [];
  int fail = 0;

  Future<GatewayChannel> call(Uri url, Map<String, String> h) async {
    urls.add(url);
    headers.add(h);
    if (fail > 0) {
      fail--;
      throw StateError('no route to host');
    }
    final c = FakeChannel();
    channels.add(c);
    return c;
  }

  FakeChannel get channel => channels.last;
}

class FakePlayer implements PcmPlayer {
  final List<String> events = [];
  final List<int> received = [];
  final List<double> gains = [];

  @override
  Future<void> begin(int sampleRate) async => events.add('begin $sampleRate');

  @override
  void add(Uint8List pcm16) {
    events.add('add');
    received.addAll(pcm16);
  }

  @override
  Future<void> finish() async => events.add('finish');

  @override
  Future<void> cancel() async => events.add('cancel');

  @override
  void setGain(double gain) => gains.add(gain);

  double get gain => gains.isEmpty ? 1.0 : gains.last;
}

/// The phone's audio routing in memory: what it was asked, and what it answers.
class FakeRouter implements AudioRouter {
  final List<String> calls = [];
  bool permission = true;
  bool grantWhenAsked = true;
  bool bluetoothPresent = true;
  AudioReport report = const AudioReport(android: '14', model: 'Test', echoCancel: true, noiseSuppress: true, echoCancelOnCapture: 'yes', bluetoothPermission: 'granted', bluetoothOn: 'on', mode: 'normal');

  @override
  VoidCallback? onDevicesChanged;

  @override
  Future<bool> hasBluetoothPermission() async => permission;

  @override
  Future<bool> requestBluetoothPermission() async {
    calls.add('request');
    if (grantWhenAsked) permission = true;
    return permission;
  }

  RouteOutcome _go(AudioRoute r) => r == AudioRoute.bluetooth && !bluetoothPresent ? const RouteOutcome(reason: 'no_device') : const RouteOutcome();

  @override
  Future<RouteOutcome> startSession(AudioRoute route) async {
    calls.add('start ${route.name}');
    return _go(route);
  }

  @override
  Future<RouteOutcome> setRoute(AudioRoute route) async {
    calls.add('route ${route.name}');
    return _go(route);
  }

  @override
  Future<void> stopSession() async => calls.add('stop');

  @override
  Future<AudioReport> probe() async {
    calls.add('probe');
    return report;
  }
}

class FakeMic implements MicCapture {
  bool allowed = true;
  bool running = false;
  int starts = 0;
  StreamController<Uint8List>? _out;

  @override
  Future<Stream<Uint8List>?> start() async {
    starts++;
    if (!allowed) return null;
    running = true;
    _out = StreamController<Uint8List>();
    return _out!.stream;
  }

  @override
  Future<void> stop() async {
    running = false;
    _out?.close();                // not awaited: a stream nobody listened to never reports being closed
  }

  void speak(List<int> pcm) => _out?.add(Uint8List.fromList(pcm));
}

class FakeVideo implements VideoSession {
  FakeVideo(this.offer);
  final String offer;
  String? answer;
  bool closed = false;

  @override
  void Function(bool up)? onConnection;

  @override
  Future<String> createOffer() async => offer;

  @override
  Future<void> setAnswer(String sdp) async => answer = sdp;

  @override
  Widget view() => const SizedBox(key: ValueKey('avatar-video'), width: 10, height: 10);

  @override
  Future<void> close() async => closed = true;
}

const sampleOffer = 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n'
    'm=video 9 UDP/TLS/RTP/SAVPF 96 97 102 103\r\na=rtpmap:96 VP8/90000\r\na=rtpmap:97 rtx/90000\r\na=rtpmap:102 H264/90000\r\na=rtpmap:103 rtx/90000\r\n';

class FakeDevices implements AvatarDevices {
  FakeDevices({this.video = true});
  final bool video;
  final FakePlayer fakePlayer = FakePlayer();
  final FakeMic fakeMic = FakeMic();
  final FakeRouter fakeRouter = FakeRouter();
  final List<FakeVideo> sessions = [];

  @override
  AudioRouter get router => fakeRouter;

  @override
  PcmPlayer get player => fakePlayer;

  @override
  MicCapture get mic => fakeMic;

  @override
  bool get videoSupported => video;

  @override
  VideoSession? newVideo() {
    if (!video) return null;
    final s = FakeVideo(sampleOffer);
    sessions.add(s);
    return s;
  }
}

/// The server's `GET /api/voice/gateway`, in memory.
class FakeGatewayApi extends Api {
  FakeGatewayApi(super.settings);
  Map<String, dynamic> info = {
    'available': true,
    'path': '/api/voice/gateway/ws',
    'personas': [
      {'slug': 'bella', 'name': 'Bella'},
      {'slug': 'otto', 'name': 'Otto'}
    ],
  };
  int calls = 0;
  bool down = false;

  @override
  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query}) async {
    calls++;
    if (down) throw ApiError(0, 'no answer');
    if (path == '/api/voice/gateway') return info;
    throw ApiError(404, 'not found');
  }
}
