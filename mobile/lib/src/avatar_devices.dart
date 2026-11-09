import 'dart:typed_data';

import 'package:flutter/widgets.dart';

import 'audio_route.dart';

/// What the Avatar tab needs from the phone, as plain interfaces: playing the PC's voice, the microphone, and the avatar's video.
/// The real ones are in avatar_platform.dart; the tests use stand-ins.
abstract class PcmPlayer {
  /// Starts a reply: 16-bit mono samples at `sampleRate` follow through `add`.
  Future<void> begin(int sampleRate);
  void add(Uint8List pcm16);

  /// The reply is complete: what was added is played to its end.
  Future<void> finish();

  /// Drops what has not been played yet.
  Future<void> cancel();

  /// The level of the voice as a fraction of the normal one (1 is normal); it applies to what is played from now on.
  void setGain(double gain);
}

abstract class MicCapture {
  /// 16-bit mono PCM at 16 kHz taken from the communication source with the phone's echo canceller and noise suppressor asked for,
  /// or null when the phone's permission was refused.
  Future<Stream<Uint8List>?> start();
  Future<void> stop();
}

abstract class VideoSession {
  /// The offer to send to the PC: receive-only video (H.264 first) and audio.
  Future<String> createOffer();
  Future<void> setAnswer(String sdp);

  /// The picture of the avatar; shown while the stream is live.
  Widget view();

  /// Called when the media connection is up (true) or has dropped (false).
  void Function(bool up)? onConnection;
  Future<void> close();
}

abstract class AvatarDevices {
  PcmPlayer get player;
  MicCapture get mic;

  /// Where the sound goes (Bluetooth, speaker or earpiece) and the audio test.
  AudioRouter get router;

  /// Whether this build can show the PC's video.
  bool get videoSupported;

  /// A new video session, or null when `videoSupported` is false.
  VideoSession? newVideo();
}
