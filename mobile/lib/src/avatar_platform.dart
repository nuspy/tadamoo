// The phone's side of the Avatar tab: the speaker, the microphone and the WebRTC video, over three plugins (flutter_pcm_sound, record,
// flutter_webrtc). Nothing else imports them, so the rest of the app and its tests do not need them.
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/widgets.dart';
import 'package:flutter_pcm_sound/flutter_pcm_sound.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:record/record.dart';

import 'avatar_devices.dart';

class PlatformAvatarDevices implements AvatarDevices {
  final _PcmOut _player = _PcmOut();
  final _Mic _mic = _Mic();

  @override
  PcmPlayer get player => _player;

  @override
  MicCapture get mic => _mic;

  @override
  bool get videoSupported => true;

  @override
  VideoSession? newVideo() => _WebRtcVideo();
}

/// The PC's voice: 16-bit mono samples handed to the plugin as they arrive; the plugin queues them and plays them in order.
class _PcmOut implements PcmPlayer {
  int _rate = 0;
  bool _started = false;

  @override
  Future<void> begin(int sampleRate) async {
    if (_rate != sampleRate) {
      await FlutterPcmSound.setup(sampleRate: sampleRate, channelCount: 1);
      _rate = sampleRate;
    }
    _started = false;
  }

  @override
  void add(Uint8List pcm16) {
    if (_rate == 0 || pcm16.length < 2) return;
    final data = ByteData.sublistView(pcm16);
    final samples = List<int>.generate(pcm16.length ~/ 2, (i) => data.getInt16(i * 2, Endian.little), growable: false);
    FlutterPcmSound.feed(PcmArrayInt16.fromList(samples));
    if (!_started) {
      _started = true;
      FlutterPcmSound.start();
    }
  }

  @override
  Future<void> finish() async {}

  /// Releasing the player drops what was queued; the next reply sets it up again.
  @override
  Future<void> cancel() async {
    if (_rate == 0) return;
    await FlutterPcmSound.release();
    _rate = 0;
    _started = false;
  }
}

class _Mic implements MicCapture {
  final AudioRecorder _recorder = AudioRecorder();

  @override
  Future<Stream<Uint8List>?> start() async {
    if (!await _recorder.hasPermission()) return null;
    return _recorder.startStream(const RecordConfig(encoder: AudioEncoder.pcm16bits, sampleRate: 16000, numChannels: 1, echoCancel: true, noiseSuppress: true));
  }

  @override
  Future<void> stop() async {
    if (await _recorder.isRecording()) await _recorder.stop();
  }
}

/// A receive-only WebRTC connection: the avatar's picture and, when the PC puts the voice on the media track, its sound.
class _WebRtcVideo implements VideoSession {
  RTCPeerConnection? _pc;
  final RTCVideoRenderer _renderer = RTCVideoRenderer();

  @override
  void Function(bool up)? onConnection;

  @override
  Future<String> createOffer() async {
    await _renderer.initialize();
    final pc = await createPeerConnection({'iceServers': <Map<String, dynamic>>[], 'sdpSemantics': 'unified-plan'});
    _pc = pc;
    pc.onTrack = (RTCTrackEvent e) {
      if (e.track.kind == 'video' && e.streams.isNotEmpty) _renderer.srcObject = e.streams.first;
    };
    pc.onConnectionState = (RTCPeerConnectionState s) => onConnection?.call(s == RTCPeerConnectionState.RTCPeerConnectionStateConnected);
    final gathered = Completer<void>();
    pc.onIceGatheringState = (RTCIceGatheringState s) {
      if (s == RTCIceGatheringState.RTCIceGatheringStateComplete && !gathered.isCompleted) gathered.complete();
    };
    await pc.addTransceiver(kind: RTCRtpMediaType.RTCRtpMediaTypeVideo, init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly));
    await pc.addTransceiver(kind: RTCRtpMediaType.RTCRtpMediaTypeAudio, init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly));
    await pc.setLocalDescription(await pc.createOffer());
    // The PC does not take candidates one by one: the offer goes out once they are all in, or after three seconds.
    await gathered.future.timeout(const Duration(seconds: 3), onTimeout: () {});
    final local = await pc.getLocalDescription();
    return local?.sdp ?? '';
  }

  @override
  Future<void> setAnswer(String sdp) async {
    await _pc!.setRemoteDescription(RTCSessionDescription(sdp, 'answer'));
    try {
      await Helper.setSpeakerphoneOn(true);
    } catch (_) {}
  }

  @override
  Widget view() => RTCVideoView(_renderer, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain);

  @override
  Future<void> close() async {
    onConnection = null;
    _renderer.srcObject = null;
    await _pc?.close();
    _pc = null;
    await _renderer.dispose();
  }
}
