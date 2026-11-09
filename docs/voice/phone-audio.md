# The phone's audio: routes, echo and full duplex

What the Avatar tab does with the phone's audio, what was checked in the plugins' real sources (`record` 6.2.1 with `record_android` 1.5.2,
`flutter_pcm_sound` 3.3.3), and what is not proven without a phone. The code is `mobile/lib/src/audio_route*.dart`, `barge_in.dart` and
`mobile/android/.../AudioRouteChannel.kt`, `CommPlayer.kt`. The protocol side is in `gateway.md` (Interruptions).

## What the plugins really do

`record` (microphone):

- `RecordConfig.echoCancel`, `noiseSuppress` and `autoGain` attach Android's `AcousticEchoCanceler`, `NoiseSuppressor` and
  `AutomaticGainControl` to the capture's audio session, only when `isAvailable()` is true. When it is false nothing is attached and only a
  log line says so: the flag is a request, not a guarantee.
- `androidConfig.audioSource` defaults to `defaultSource`. `voiceCommunication` is offered and is the source those effects are made for.
- `androidConfig.audioManagerMode` and `speakerphone` change the mode and the speakerphone before recording and put them back after; both
  are off by default.
- `androidConfig.manageBluetooth` defaults to true and starts Bluetooth SCO by itself. The app sets it to false: the routing is one thing,
  in `AudioRouteChannel`.

`flutter_pcm_sound` (the PC's voice):

- It builds its `AudioTrack` with `USAGE_MEDIA` and `CONTENT_TYPE_MUSIC` and nothing in its API changes that (`setup`, `feed`, `start`,
  `release`, a feed threshold; no usage, no session id, no volume).
- So its output is media audio, not communication audio. The communication device chosen with `AudioManager.setCommunicationDevice` (or
  `setSpeakerphoneOn`) is the route of communication audio; media audio is not required to follow it, so an earpiece route is unlikely to
  work with this player. The phone's echo canceller also takes its reference from the communication playback path; whether it sees a media
  track is up to the device.
- It has no volume, so a lowered level can only be applied to samples before they are handed over, which leaves the audio already queued
  at full level.

## What the app does about it

- A voice session (the Avatar tab is shown) puts the phone in `MODE_IN_COMMUNICATION` and applies the route; leaving the tab puts the mode
  and the route back. API 31 and later use `setCommunicationDevice` / `clearCommunicationDevice` over `availableCommunicationDevices`;
  before that `setSpeakerphoneOn` and `startBluetoothSco`.
- Capture uses the `voiceCommunication` source with echo cancellation and noise suppression requested and Bluetooth management off.
- The PC's voice is played by `CommPlayer.kt`: an `AudioTrack` with `USAGE_VOICE_COMMUNICATION`, so it follows the route and is what the
  echo canceller expects, and `setVolume` lowers what is already queued. If the channel or the track is missing, `flutter_pcm_sound` plays
  instead, with the level applied to the samples.
- Bluetooth needs `BLUETOOTH_CONNECT` on Android 12 and later. It is asked for only when the Bluetooth icon is tapped, after a dialog that
  says why.

## Full duplex: what is allowed

| Route | Default | Rule |
|---|---|---|
| Bluetooth headset | full duplex | the headset keeps the speaker away from its own microphone |
| Speakerphone | half duplex | full duplex only when the audio test found an echo canceller and the person switched it on |
| Earpiece and phone microphone | half duplex | the earpiece is close to the microphone; never full duplex |

Half duplex means the microphone is closed while the avatar speaks; the hold button interrupts. Full duplex keeps the microphone open for a
local voice detector (`BargeIn`: 150 ms of speech to start, 700 ms of quiet to end); at the start the avatar goes to 30 percent and the PC
decides.

Whether the speakerphone can really be full duplex with the avatar's own voice and the room's noise filtered depends on the phone's echo
canceller: run the audio test. Nothing here claims it works. A canceller that is present may still leave echo, and the detector would then
take the avatar's own voice for the person.

## The audio test

Settings > Avatar and voice > Audio test shows plain text to copy and send: Android version and model, `AcousticEchoCanceler`,
`NoiseSuppressor` and `AutomaticGainControl` availability, whether the echo canceller attaches to a `VOICE_COMMUNICATION` capture
(needs the microphone permission), the Bluetooth permission and state, the audio mode, the current communication device and the list of
communication devices. A report with an echo canceller unlocks the speakerphone's full-duplex switch.

## Not proven without a phone

- That the earpiece, the speaker and the Bluetooth route move the voice of `CommPlayer` as expected on each device.
- Bluetooth SCO start and the headset's microphone on API 31 and later, and the fallback before it.
- That the echo canceller removes the avatar's voice from the microphone on the speakerphone, and how much.
- That the Kotlin sources compile: they were written without a build.
- Whether the detector's threshold (RMS 0.03) is right for every phone and room.
